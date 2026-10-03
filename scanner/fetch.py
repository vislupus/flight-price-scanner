"""HTTP заявки към сайтовете на авиокомпаниите.

И двата сайта имат защита срещу ботове, която понякога отказва заявки, ако
не приличат на истински браузър – включително на ниво TLS. Затова се пробват
няколко начина по ред; първият успешен се запомня и се ползва първи нататък:

  1. curl_cffi, имитиращ Chrome изцяло (TLS + HTTP/2 + заглавки)
  2. curl_cffi, имитиращ Safari
  3. обикновен requests (както работеше старият скрипт)

Всеки начин има своя сесия (бисквитки), защото Wizz Air издава token за
сесията – затова авиокомпанията може да подаде `prepare`, което се
изпълнява веднъж за всяка сесия и връща допълнителни заглавки.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import requests

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # локално без curl_cffi – остава само requests
    cffi_requests = None

LANG = {"Accept-Language": "en-GB,en;q=0.9,bg;q=0.8"}


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """Сайтът отказа достъп – има смисъл да се пробва друг начин."""


class Response:
    """Еднакъв вид отговор, независимо от библиотеката."""

    def __init__(self, status: int, text: str, headers: dict):
        self.status = status
        self.text = text
        self.headers = {k.lower(): v for k, v in headers.items()}

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError as e:
            raise FetchError(f"Отговорът не е JSON ({e}): {self.text[:120]!r}") from e


def describe_block(resp: Response) -> str:
    parts = [f"HTTP {resp.status}"]
    if resp.headers.get("server"):
        parts.append(f"server={resp.headers['server']}")
    for key in ("cf-mitigated", "x-akamai-session-info", "x-iinfo", "x-datadome", "x-kpsdk-ct"):
        if key in resp.headers:
            parts.append(f"{key}={str(resp.headers[key])[:40]}")
    m = re.search(r"<title[^>]*>(.*?)</title>", resp.text or "", re.S | re.I)
    if m:
        parts.append(f"title='{' '.join(m.group(1).split())[:60]}'")
    elif resp.text and len(resp.text) < 200:
        parts.append(f"body={resp.text.strip()[:80]!r}")
    return ", ".join(parts)


class Strategy:
    def __init__(self, name: str, session):
        self.name = name
        self.session = session
        self.state: dict[str, Any] = {}   # напр. token за Wizz Air

    def request(self, method: str, url: str, *, params=None, json_body=None,
                headers=None, timeout=30) -> Response:
        r = self.session.request(method, url, params=params, json=json_body,
                                 headers=headers, timeout=timeout, allow_redirects=True)
        return Response(r.status_code, r.text, dict(r.headers))

    def cookie(self, name: str) -> str | None:
        try:
            return self.session.cookies.get(name)
        except Exception:  # noqa: BLE001
            return None


def make_strategies() -> list[Strategy]:
    out = []
    if cffi_requests is not None:
        for browser in ("chrome", "safari"):
            s = cffi_requests.Session(impersonate=browser)
            s.headers.update(LANG)
            out.append(Strategy(f"curl_cffi/{browser}", s))
    s = requests.Session()
    s.headers.update({
        **LANG,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    })
    out.append(Strategy("requests", s))
    return out


class Fetcher:
    def __init__(self, timeout: float = 30, log: Callable[[str], None] = print):
        self.timeout = timeout
        self.log = log
        self.strategies = make_strategies()
        self.last_used: str | None = None

    def _once(self, strategy: Strategy, method: str, url: str, *, params, json_body,
              headers, ok_statuses) -> Response:
        last = "неизвестна грешка"
        for attempt in range(3):
            try:
                resp = strategy.request(method, url, params=params, json_body=json_body,
                                        headers=headers, timeout=self.timeout)
            except Exception as e:  # noqa: BLE001 – различни библиотеки, различни изключения
                last = f"Мрежова грешка: {e}"
                time.sleep(3 * (attempt + 1))
                continue
            if resp.status in ok_statuses:
                return resp
            if resp.status in (401, 403):
                raise BlockedError(describe_block(resp))
            if resp.status == 429:
                # Kasada/CloudFront отговарят 429 с празно тяло, когато не харесват клиента
                if not (resp.text or "").strip():
                    raise BlockedError(describe_block(resp))
                last = describe_block(resp)
                time.sleep(10 * (attempt + 1))
                continue
            if resp.status in (502, 503, 504) or resp.status >= 500:
                last = describe_block(resp)
                time.sleep(5 * (attempt + 1))
                continue
            raise FetchError(f"Сайтът върна {describe_block(resp)}.")
        if "HTTP 429" in last or "HTTP 503" in last:
            raise BlockedError(last)
        raise FetchError(last)

    def request(self, method: str, url: str, *, params: dict | None = None,
                json_body: Any = None, headers: dict | None = None,
                prepare: Callable[[Strategy], dict] | None = None,
                ok_statuses=(200,)) -> Response:
        """Пробва начините по ред. `prepare(strategy)` се вика веднъж за сесия и
        връща заглавки, които се добавят към заявката (напр. token)."""
        blocked = []
        for strategy in list(self.strategies):
            extra = {}
            if prepare is not None:
                try:
                    extra = prepare(strategy)
                except BlockedError as e:
                    blocked.append(f"{strategy.name}: {e}")
                    continue
            try:
                resp = self._once(strategy, method, url, params=params, json_body=json_body,
                                  headers={**(headers or {}), **extra}, ok_statuses=ok_statuses)
            except BlockedError as e:
                blocked.append(f"{strategy.name}: {e}")
                continue
            if strategy is not self.strategies[0]:
                # запомняме работещия начин за следващите заявки
                self.strategies.remove(strategy)
                self.strategies.insert(0, strategy)
            if self.last_used != strategy.name:
                self.log(f"Изтегляне чрез {strategy.name}")
                self.last_used = strategy.name
            return resp
        reasons = {b.split(": ", 1)[1] for b in blocked}
        detail = reasons.pop() if len(reasons) == 1 else " | ".join(blocked)
        raise BlockedError(f"Сайтът блокира достъпа ({detail})")

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, json_body: Any, **kw) -> Response:
        return self.request("POST", url, json_body=json_body, **kw)
