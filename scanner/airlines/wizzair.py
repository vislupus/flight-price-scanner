"""Wizz Air – вътрешният API на сайта (be.wizzair.com).

Как работи сайтът им (и ние повтаряме същото):
  1. Началната страница съдържа адреса на API-то с текущата версия,
     напр. apiUrl:"https://be.wizzair.com/29.14.0/Api". Версията се сменя
     често, затова се чете при всяка проверка (резервно – metadata.json).
  2. GET {api}/userSession/new дава бисквитка RequestVerificationToken,
     която се праща обратно като заглавие X-RequestVerificationToken.
  3. POST {api}/search/timetableV2 връща за период до ~1 месец най-евтината
     цена за всеки ден с полет, в двете посоки наведнъж:
       {"outboundFlights": [{"departureStation": "SOF", "arrivalStation": "LTN",
                             "departureDate": "2026-11-05T00:00:00",
                             "price": {"amount": 39.99, "currencyCode": "EUR"},
                             "departureDates": [{"date": "2026-11-05T06:30:00",
                                                 "isCheapestOfTheDay": true}]}, ...],
        "returnFlights": [...]}
     Дни без полет просто липсват; price: null = няма цена.

Търсенето на конкретен полет (search/search) е зад защитата Kasada и не се
ползва. Цените са във валутата на летището на тръгване.
"""
from __future__ import annotations

import calendar
import re

from ..db import Fare, today_sofia
from ..fetch import BlockedError, FetchError, Strategy
from . import MonthFares, RouteNotServed

HOMEPAGE = "https://www.wizzair.com/en-gb"
METADATA = "https://wizzair.com/static_fe/metadata.json"
API_HOST = "https://be.wizzair.com"
DEFAULT_VERSION = "29.14.0"
TOKEN_COOKIE = "RequestVerificationToken"
TOKEN_HEADER = "X-RequestVerificationToken"
VERSION_RE = re.compile(r"be\.wizzair\.com/(\d+\.\d+\.\d+)/Api")
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.wizzair.com",
    "Referer": HOMEPAGE,
}
NOT_SERVED_CODES = {"InvalidMarket", "NoFlightsFound", "InvalidStation", "StationNotFound",
                    "RouteNotFound", "InvalidRoute", "NoRouteFound"}


def _hhmm(value: str | None) -> str | None:
    if not value or "T" not in value:
        return None
    return value.split("T", 1)[1][:5]


def parse_flights(items: list | None, rates) -> dict[str, Fare]:
    out: dict[str, Fare] = {}
    for item in items or []:
        day = (item.get("departureDate") or "")[:10]
        if not day:
            continue
        price = item.get("price") or {}
        amount = price.get("amount")
        # Разпродадените дни идват с amount: 0 (priceType "soldOut"/"checkPrice") – това не е цена
        if amount is None or float(amount) <= 0 or item.get("priceType") in ("soldOut", "checkPrice", "noData"):
            out[day] = Fare(price=None)
            continue
        currency = price.get("currencyCode")
        deps = item.get("departureDates") or []
        cheapest = next((d for d in deps if d.get("isCheapestOfTheDay")), deps[0] if deps else {})
        out[day] = Fare(
            price=float(amount), currency=currency,
            price_eur=rates.to_eur(float(amount), currency),
            dep_time=_hhmm(cheapest.get("date")),
        )
    return out


class WizzAir:
    key = "wizzair"
    name = "Wizz Air"

    def __init__(self, fetcher, rates, log=print):
        self.fetcher = fetcher
        self.rates = rates
        self.log = log
        self.version: str | None = None

    # ---- сесия ----------------------------------------------------------

    def _discover_version(self, strategy: Strategy) -> str:
        for url in (HOMEPAGE, METADATA):
            try:
                resp = strategy.request("GET", url, headers={"Accept": "*/*"}, timeout=30)
            except Exception as e:  # noqa: BLE001
                self.log(f"Wizz Air: {url} не се отвори ({e})")
                continue
            if resp.status in (401, 403) or (resp.status == 429 and not resp.text.strip()):
                raise BlockedError(f"Wizz Air блокира {url}: HTTP {resp.status}")
            m = VERSION_RE.search(resp.text or "")
            if m:
                return m.group(1)
        self.log(f"Wizz Air: версията на API-то не се намери, ползвам {DEFAULT_VERSION}")
        return DEFAULT_VERSION

    def _ensure_version(self) -> None:
        """Открива версията на API-то през първия начин, който не е блокиран."""
        if self.version is not None:
            return
        blocked = []
        for strategy in list(self.fetcher.strategies):
            try:
                self.version = self._discover_version(strategy)
                return
            except BlockedError as e:
                blocked.append(f"{strategy.name}: {e}")
        raise BlockedError("Wizz Air блокира достъпа (" + " | ".join(blocked) + ")")

    def _prepare(self, strategy: Strategy) -> dict:
        """Веднъж за сесия: token за заявките. Връща заглавките за заявките."""
        st = strategy.state.setdefault("wizzair", {})
        if "headers" in st:
            return st["headers"]
        base = self.base
        try:
            resp = strategy.request("GET", f"{base}/userSession/new", headers=HEADERS, timeout=30)
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"Wizz Air: userSession/new не отговори ({e})") from e
        if resp.status in (401, 403) or (resp.status == 429 and not resp.text.strip()):
            raise BlockedError(f"Wizz Air блокира userSession/new: HTTP {resp.status}")
        token = strategy.cookie(TOKEN_COOKIE)
        headers = dict(HEADERS)
        if token:
            headers[TOKEN_HEADER] = token
        else:
            self.log("Wizz Air: няма RequestVerificationToken – продължавам без него")
        st["headers"] = headers
        st["base"] = base
        return headers

    def _headers(self, strategy: Strategy) -> dict:
        headers = dict(self._prepare(strategy))
        token = strategy.cookie(TOKEN_COOKIE)   # token-ът се сменя – четем го прясно
        if token:
            headers[TOKEN_HEADER] = token
        return headers

    @property
    def base(self) -> str:
        return f"{API_HOST}/{self.version or DEFAULT_VERSION}/Api"

    # ---- цени -----------------------------------------------------------

    def scan_month(self, origin: str, destination: str, year: int, month: int) -> MonthFares:
        # API-то отказва период, започващ в миналото (InvalidFromDate)
        first = max(f"{year}-{month:02d}-01", today_sofia().isoformat())
        last = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
        if first > last:
            return MonthFares()
        body = {
            "flightList": [
                {"departureStation": origin, "arrivalStation": destination, "from": first, "to": last},
                {"departureStation": destination, "arrivalStation": origin, "from": first, "to": last},
            ],
            "priceType": "regular",
            "adultCount": 1,
            "childCount": 0,
            "infantCount": 0,
        }
        # Версията може да се е сменила между две проверки – при 404 я откриваме наново
        for attempt in range(2):
            self._ensure_version()
            resp = self.fetcher.post(
                f"{self.base}/search/timetableV2", body, prepare=self._headers,
                ok_statuses=(200, 400, 404),
            )
            if resp.status == 404 and attempt == 0:
                self.log("Wizz Air: API-то върна 404 – проверявам версията наново")
                self.version = None
                for s in self.fetcher.strategies:
                    s.state.pop("wizzair", None)
                continue
            break
        if resp.status == 404:
            raise FetchError("Wizz Air API върна 404 – вероятно е сменен адресът на API-то.")
        data = resp.json()
        if resp.status == 400:
            codes = data.get("validationCodes") if isinstance(data, dict) else None
            if codes and set(codes) & NOT_SERVED_CODES:
                raise RouteNotServed(f"Wizz Air не лети {origin}-{destination} ({', '.join(codes)})")
            raise FetchError(f"Wizz Air отхвърли заявката: {codes or data}")
        if not isinstance(data, dict) or "outboundFlights" not in data:
            raise FetchError(f"Неочакван отговор от Wizz Air: {resp.text[:150]!r}")
        return MonthFares(
            outbound=parse_flights(data.get("outboundFlights"), self.rates),
            inbound=parse_flights(data.get("returnFlights"), self.rates),
        )
