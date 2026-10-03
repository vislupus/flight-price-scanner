"""Валутни курсове към евро.

Ryanair и Wizz Air връщат цените във валутата на летището на тръгване
(SOF → EUR, STN → GBP, WRO → PLN …). За да се сравняват и събират, всичко
се превръща в евро. Курсовете идват от ЕЦБ през frankfurter.app (без ключ);
ако няма връзка, се ползват последните записани в базата, а накрая –
вградените приблизителни стойности.
"""
from __future__ import annotations

import json
from datetime import date

import requests

FIXED = {"EUR": 1.0, "BGN": 1.95583, "BAM": 1.95583}
DEFAULTS = {
    "GBP": 0.86, "PLN": 4.27, "HUF": 400.0, "CZK": 24.8, "RON": 5.0, "DKK": 7.46,
    "SEK": 11.0, "NOK": 11.6, "CHF": 0.94, "USD": 1.08, "ILS": 3.9, "MAD": 10.7,
    "AED": 3.97, "GEL": 2.9, "RSD": 117.0, "MKD": 61.5, "ALL": 98.0, "ISK": 150.0,
    "TRY": 40.0, "EGP": 52.0, "SAR": 4.05, "JOD": 0.77, "AMD": 420.0, "AZN": 1.84,
    "KZT": 520.0, "UZS": 13500.0, "MDL": 19.0, "UAH": 44.0, "CAD": 1.48,
}
FX_URL = "https://api.frankfurter.app/latest?from=EUR"


class Rates:
    def __init__(self, rates: dict[str, float], source: str, as_of: str | None = None):
        self.rates = {**DEFAULTS, **rates, **FIXED}
        self.source = source
        self.as_of = as_of
        self.unknown: set[str] = set()

    @classmethod
    def load(cls, db=None, fetch: bool = True, log=print) -> "Rates":
        if fetch:
            try:
                r = requests.get(FX_URL, timeout=15)
                data = r.json()
                if r.ok and isinstance(data.get("rates"), dict) and data["rates"]:
                    rates = {k: float(v) for k, v in data["rates"].items()}
                    if db is not None:
                        db.set_meta("fx_rates", json.dumps({"date": data.get("date"), "rates": rates}))
                    return cls(rates, "ЕЦБ", data.get("date"))
            except (requests.RequestException, ValueError) as e:
                log(f"Курсовете не можаха да се изтеглят ({e}); ползвам запазените.")
        if db is not None:
            saved = db.get_meta("fx_rates")
            if saved:
                try:
                    data = json.loads(saved)
                    return cls(data["rates"], "запазени", data.get("date"))
                except (ValueError, KeyError, TypeError):
                    pass
        return cls({}, "вградени", None)

    def to_eur(self, amount: float | None, currency: str | None) -> float | None:
        if amount is None:
            return None
        cur = (currency or "EUR").upper()
        rate = self.rates.get(cur)
        if rate is None:
            self.unknown.add(cur)
            return None
        return round(amount / rate, 2)

    def describe(self) -> str:
        return f"{self.source}" + (f" от {self.as_of}" if self.as_of else "")


def today_iso() -> str:
    return date.today().isoformat()
