"""Ryanair – публичният „fare finder“, който ползва и сайтът им.

GET https://www.ryanair.com/api/farfnd/v4/roundTripFares/SOF/BCN/cheapestPerDay
      ?outboundMonthOfDate=2026-11-01&inboundMonthOfDate=2026-11-01&market=en-gb

Връща за всеки ден от месеца най-евтиния полет в двете посоки:
  {"outbound": {"fares": [{"day": "2026-11-01", "departureDate": "...T06:40:00",
                            "arrivalDate": "...", "price": {"value": 37.99,
                            "currencyCode": "EUR"}, "soldOut": false, "unavailable": false}, ...]},
   "inbound": {...}}
Дни без полет имат "unavailable": true и price: null.
"""
from __future__ import annotations

from ..db import Fare
from ..fetch import FetchError
from . import MonthFares, RouteNotServed

API = "https://www.ryanair.com/api/farfnd/v4/roundTripFares/{origin}/{destination}/cheapestPerDay"
MARKET = "en-gb"   # същият market, който ползваше старият скрипт – за да е сравнима историята
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.ryanair.com/gb/en/trip/flights/select",
}


def _hhmm(value: str | None) -> str | None:
    if not value or "T" not in value:
        return None
    return value.split("T", 1)[1][:5]


def parse_day_fares(block: dict | None, rates) -> dict[str, Fare]:
    """Превръща outbound/inbound блока в {дата: Fare}."""
    out: dict[str, Fare] = {}
    if not block:
        return out
    for item in block.get("fares") or []:
        day = item.get("day")
        if not day:
            continue
        price = item.get("price") or {}
        value = price.get("value")
        if item.get("unavailable") and value is None:
            continue   # няма полет този ден
        if value is None or float(value) <= 0:
            out[day] = Fare(price=None)   # разпродаден или без цена
            continue
        currency = price.get("currencyCode")
        out[day] = Fare(
            price=float(value), currency=currency,
            price_eur=rates.to_eur(float(value), currency),
            dep_time=_hhmm(item.get("departureDate")),
            arr_time=_hhmm(item.get("arrivalDate")),
        )
    return out


class Ryanair:
    key = "ryanair"
    name = "Ryanair"

    def __init__(self, fetcher, rates, log=print):
        self.fetcher = fetcher
        self.rates = rates
        self.log = log
        self.currency_seen: str | None = None

    def scan_month(self, origin: str, destination: str, year: int, month: int) -> MonthFares:
        first = f"{year}-{month:02d}-01"
        resp = self.fetcher.get(
            API.format(origin=origin, destination=destination),
            params={"outboundMonthOfDate": first, "inboundMonthOfDate": first,
                    "market": MARKET, "ToUs": "AGREED"},
            headers=HEADERS, ok_statuses=(200, 404),
        )
        if resp.status == 404:
            raise RouteNotServed(f"Ryanair не лети {origin}-{destination} (API върна 404)")
        data = resp.json()
        if not isinstance(data, dict) or "outbound" not in data:
            raise FetchError(f"Неочакван отговор от Ryanair: {resp.text[:150]!r}")
        result = MonthFares(
            outbound=parse_day_fares(data.get("outbound"), self.rates),
            inbound=parse_day_fares(data.get("inbound"), self.rates),
        )
        for fares in (result.outbound, result.inbound):
            for f in fares.values():
                if f.currency and not self.currency_seen:
                    self.currency_seen = f.currency
        return result
