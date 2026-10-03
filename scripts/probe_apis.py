"""Проверява дали API-тата на Ryanair и Wizz Air отговарят от текущата машина
(напр. от GitHub Actions) – същите заявки, които прави и скенерът.

    python scripts/probe_apis.py                 # SOF-BCN (Ryanair), SOF-LTN (Wizz Air)
    python scripts/probe_apis.py SOF BCN         # конкретен маршрут и за двете
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scanner.airlines import airline_for  # noqa: E402
from scanner.fetch import Fetcher  # noqa: E402
from scanner.fx import Rates  # noqa: E402


def main() -> None:
    args = [a.upper() for a in sys.argv[1:]]
    pairs = {"ryanair": ("SOF", "BCN"), "wizzair": ("SOF", "LTN")}
    if len(args) == 2:
        pairs = {"ryanair": (args[0], args[1]), "wizzair": (args[0], args[1])}

    try:
        r = requests.get("https://api.ipify.org", timeout=10)
        ip = r.text.strip() if r.ok and re.fullmatch(r"[\d.:a-fA-F]+", r.text.strip()) else "неизвестен"
    except requests.RequestException:
        ip = "неизвестен"
    print(f"Проверка от IP {ip}\n")

    rates = Rates.load(None, log=print)
    print(f"Валутни курсове: {rates.describe()}\n")
    today = date.today()
    y, m = (today.year + (today.month // 12), today.month % 12 + 1)   # следващият месец
    results = []
    for key, (o, d) in pairs.items():
        fetcher = Fetcher(log=lambda msg: print("   ", msg))
        airline = airline_for(key, fetcher, rates, log=lambda msg: print("   ", msg))
        t0 = time.monotonic()
        try:
            res = airline.scan_month(o, d, y, m)
            priced = [f for f in res.outbound.values() if f.price is not None]
            cur = priced[0].currency if priced else "—"
            cheapest = min((f.price_eur for f in priced if f.price_eur is not None), default=None)
            status = (f"OK – {len(priced)} дни с цена отиване, {len(res.inbound)} дни връщане, "
                      f"валута {cur}, най-евтино {cheapest} €")
            ok = True
        except Exception as e:  # noqa: BLE001
            status, ok = f"ГРЕШКА – {type(e).__name__}: {e}", False
        ms = int((time.monotonic() - t0) * 1000)
        results.append((airline.name, f"{o}-{d}", ok, status, fetcher.last_used or "—", ms))
        print(f"{'✅' if ok else '⛔'} {airline.name} {o}-{d} {y}-{m:02d}: {status} ({ms} ms, {fetcher.last_used})\n")

    out = os.getenv("GITHUB_STEP_SUMMARY")
    if out:
        md = [f"## Проверка на API-тата от IP `{ip}`", "", "| Авиокомпания | Маршрут | Резултат | Начин | ms |",
              "|---|---|---|---|---:|"]
        md += [f"| {n} | {r} | {'✅' if ok else '⛔'} {s} | {how} | {ms} |" for n, r, ok, s, how, ms in results]
        with open(out, "a", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")
    sys.exit(0 if all(r[2] for r in results) else 1)


if __name__ == "__main__":
    main()
