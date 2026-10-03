"""Ежедневна проверка на цените.

    python -m scanner                 # нормално изпълнение (проверка + известия + summary.json)
    python -m scanner --dry-run       # само показва, не пише в базата и не праща Slack
    python -m scanner --no-notify     # записва, но не праща Slack
    python -m scanner export          # само пресмята docs/data/summary.json от базата
"""
from __future__ import annotations

import argparse
import calendar
import os
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from .airlines import AIRLINE_NAMES, RouteNotServed, airline_for
from .config import ConfigError, load_config
from .db import Database, today_sofia
from .export import export_summary
from .fetch import BlockedError, FetchError, Fetcher
from .fx import Rates
from .export import route_insights
from .insights import lead_bucket, pair_context, percentile_of, verdict
from .notify import SlackNotifier, deals_message, errors_message, fmt_eur
from .trips import Leg, PATTERNS, build_trips, cheapest_per_date, fmt_day

ROOT = Path(__file__).resolve().parent.parent
IN_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"


def log(level: str, msg: str) -> None:
    """В GitHub Actions ползва анотации (::notice / ::warning / ::error)."""
    icons = {"notice": "✅", "warning": "⚠️", "error": "❌", "info": "  "}
    if IN_ACTIONS and level in ("warning", "error"):
        print(f"::{level} ::{msg}")
    else:
        print(f"{icons.get(level, '')} {msg}")


def months_ahead(start: date, count: int) -> list[tuple[int, int]]:
    out = []
    y, m = start.year, start.month
    for _ in range(count):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def month_bounds(y: int, m: int) -> tuple[str, str]:
    return f"{y}-{m:02d}-01", f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def current_legs(db: Database, airlines: list[str], origin: str, destination: str,
                 date_from: str, only_as_of: str | None = None) -> dict[str, dict[str, Leg]]:
    """{авиокомпания: {дата: Leg}} с последните известни цени за една посока."""
    out: dict[str, dict[str, Leg]] = {}
    for a in airlines:
        as_of, rows = db.current_prices(a, origin, destination, date_from)
        if not rows or (only_as_of and as_of != only_as_of):
            continue
        out[a] = {d: Leg(airline=a, date=d, price_eur=r["price_eur"], price=r["price"],
                         currency=r["currency"], dep_time=r["dep_time"]) for d, r in rows.items()}
    return out


def leg_intervals(db: Database, airlines: list[str], origin: str, destination: str, flight_date: str):
    """Историята на един полет: по един списък с интервали за всяка авиокомпания."""
    sets = []
    for a in airlines:
        rows = db.flight_intervals(a, origin, destination, flight_date)
        if rows:
            sets.append(rows)
    return sets


def lead_stats_for(ins: dict, trip, lead: int) -> dict | None:
    """Статистиката за „толкова дни преди полета“ – за евтини случаи, ако и двата
    полета вече са под типичната си цена, иначе общата."""
    b = lead_bucket(lead)
    if b is None:
        return None
    lv = ins.get("levels") or {}
    cheap = (lv.get("out") and lv.get("in") and trip.out.price_eur <= lv["out"]["typical"]
             and trip.back.price_eur <= lv["in"]["typical"])
    rows = [r for r in (ins.get("lead_time") or {}).get("out", []) if (r["lo"], r["hi"]) == b]
    if not rows:
        return None
    r = rows[0]
    return (r.get("cheap") or r) if cheap else r


def find_deals(db: Database, config, today: str) -> list[dict]:
    """Комбинациите под целта, за които още не е пращано известие (или са поевтинели)."""
    deals = []
    s = config.settings
    for route in config.active_routes:
        if route.target_price is None:
            continue
        out = cheapest_per_date(current_legs(db, route.airlines, route.origin, route.destination, today, today))
        back = cheapest_per_date(current_legs(db, route.airlines, route.destination, route.origin, today, today))
        if not out or not back:
            continue
        seen: dict[str, tuple] = {}
        all_totals: list[float] = []
        ins = route_insights(db, route.airlines, route.origin, route.destination, today)
        for pattern in s.patterns:
            trips = build_trips(out, back, route.nights, pattern, date.fromisoformat(today))
            all_totals += [t.total for t in trips]
            for t in trips:
                if t.total > route.target_price:
                    break
                if t.key not in seen or t.total < seen[t.key][0].total:
                    seen[t.key] = (t, pattern)
        # Само най-евтините N под целта; известие идва, когато някоя от тях е
        # нова или е поевтиняла спрямо последното известие за същите дати.
        items = []
        top = sorted(seen.items(), key=lambda kv: kv[1][0].total)[:s.alerts_per_route]
        for key, (t, pattern) in top:
            alert_key = f"{route.key}|{key}"
            last = db.last_alert_price(alert_key)
            if last is not None and not s.repeat_alerts_daily and t.total >= last - 0.005:
                continue
            ctx = pair_context(
                leg_intervals(db, route.airlines, route.origin, route.destination, t.out.date),
                leg_intervals(db, route.airlines, route.destination, route.origin, t.back.date),
                today,
            )
            lead = (date.fromisoformat(t.out.date) - date.fromisoformat(today)).days
            v = verdict(t.total, target=route.target_price, ctx=ctx,
                        percentile=percentile_of(t.total, all_totals), lead_days=lead,
                        lead_stats=lead_stats_for(ins, t, lead), levels=ins["levels"]["total"])
            items.append({"trip": t, "verdict": v, "ctx": ctx, "pattern": pattern, "alert_key": alert_key})
        if items:
            deals.append({"route": route.key, "name": route.name, "target": route.target_price, "trips": items})
    return deals


def write_step_summary(rows: list[dict], errors: list[dict], notes: list[str]) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    out = ["## Полети днес", "", "| Маршрут | Най-евтино (дълъг уикенд) | Цел | Дати | Нови цени |",
           "|---|---:|---:|---|---:|"]
    for r in rows:
        best = r["best"]
        if best:
            hit = " 🔔" if r["target"] is not None and best.total <= r["target"] else ""
            dates = f"{fmt_day(best.out.date)} → {fmt_day(best.back.date)}"
            out.append(f"| {r['name']} | {fmt_eur(best.total)}{hit} | {fmt_eur(r['target'])} | {dates} | {r['changes']} |")
        else:
            out.append(f"| {r['name']} | — | {fmt_eur(r['target'])} | няма комбинации | {r['changes']} |")
    if notes:
        out += ["", *[f"- {n}" for n in notes]]
    if errors:
        out += ["", "### Грешки", ""]
        out += [f"- {AIRLINE_NAMES.get(e['airline'], e['airline'])} {e['leg']}: {e['error']}" for e in errors]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def run(config_path: Path, db_path: Path, dry_run: bool, notify: bool, summary_path: Path | None) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as e:
        log("error", str(e).replace("\n", " "))
        return 2

    routes = config.active_routes
    if not routes:
        log("warning", "Няма активни маршрути във flights.yaml.")
        return 0

    db = Database(db_path if not dry_run else ":memory:")
    db.sync_routes(config.routes)
    today = today_sofia()
    today_s = today.isoformat()
    scan_id = db.start_scan(today_s)

    slack = SlackNotifier()
    notify = notify and not dry_run
    if notify and not slack.mode:
        log("warning", "Slack не е настроен (липсват SLACK_BOT_TOKEN/SLACK_CHANNEL_ID "
                       "или SLACK_WEBHOOK_URL) – известията се пропускат.")
    dashboard_url = os.getenv("DASHBOARD_URL", "").strip() or None

    rates = Rates.load(db if not dry_run else None, log=lambda m: log("info", m))
    log("info", f"Валутни курсове: {rates.describe()}")
    fetcher = Fetcher(log=lambda m: log("info", m))
    airlines = {a: airline_for(a, fetcher, rates, log=lambda m: log("info", m)) for a in config.settings.airlines}
    months = months_ahead(today, config.settings.months_ahead)

    errors: list[dict] = []
    notes: list[str] = []
    blocked: dict[str, str] = {}
    ok_legs = 0
    changes_by_route: dict[str, int] = {}
    calls = 0

    for route in routes:
        changes = 0
        for a in route.airlines:
            if a in blocked:
                errors.append({"airline": a, "leg": route.key, "error": blocked[a]})
                continue
            if a not in airlines:
                airlines[a] = airline_for(a, fetcher, rates, log=lambda m: log("info", m))
            airline = airlines[a]
            leg_ok = failed = 0
            priced_out = priced_in = 0
            for (y, m) in months:
                first, last = month_bounds(y, m)
                if last < today_s:
                    continue
                if calls:
                    time.sleep(config.settings.request_delay + random.uniform(0, 0.8))
                calls += 1
                try:
                    res = airline.scan_month(route.origin, route.destination, y, m)
                except RouteNotServed as e:
                    notes.append(f"{airline.name} {route.key}: {e}")
                    log("warning", f"{airline.name} {route.key}: {e}")
                    leg_ok = -1
                    break
                except BlockedError as e:
                    blocked[a] = str(e)
                    errors.append({"airline": a, "leg": route.key, "error": str(e)})
                    log("error", f"{airline.name} {route.key}: {e}")
                    leg_ok = -1
                    break
                except FetchError as e:
                    failed += 1
                    log("error", f"{airline.name} {route.key} {y}-{m:02d}: {e}")
                    if failed >= 3:
                        errors.append({"airline": a, "leg": route.key,
                                       "error": f"{failed} месеца не се прочетоха; последно: {e}"})
                        leg_ok = -1
                        break
                    continue
                except Exception as e:  # noqa: BLE001 – не спираме заради един месец
                    failed += 1
                    log("error", f"{airline.name} {route.key} {y}-{m:02d}: неочаквана грешка {e!r}")
                    if failed >= 3:
                        errors.append({"airline": a, "leg": route.key, "error": f"{type(e).__name__}: {e}"})
                        leg_ok = -1
                        break
                    continue
                start = max(first, today_s)
                out = {d: f for d, f in res.outbound.items() if start <= d <= last}
                inb = {d: f for d, f in res.inbound.items() if start <= d <= last}
                s1 = db.record_leg(a, route.origin, route.destination, today_s, out, start, last)
                s2 = db.record_leg(a, route.destination, route.origin, today_s, inb, start, last)
                changes += s1.new + s1.gone + s2.new + s2.gone
                priced_out += s1.priced
                priced_in += s2.priced
                leg_ok += 1
            if leg_ok > 0:
                ok_legs += 1
                if priced_out + priced_in == 0:
                    notes.append(f"{airline.name} {route.key}: няма нито една цена за цялата година "
                                 f"(вероятно не лети по този маршрут)")
                log("notice", f"{airline.name} {route.key}: {priced_out} дни отиване, {priced_in} дни връщане, "
                              f"{leg_ok} месеца" + (f", {failed} с грешка" if failed else ""))
            elif leg_ok == 0 and failed:
                errors.append({"airline": a, "leg": route.key, "error": "нито един месец не се прочете"})
        changes_by_route[route.key] = changes

    # Старият CSV не пазеше валута – научаваме я от първия истински отговор
    ry = airlines.get("ryanair")
    if ry is not None and getattr(ry, "currency_seen", None) and not dry_run:
        cur = ry.currency_seen
        rate = rates.rates.get(cur)
        if rate:
            n = db.resolve_legacy_currency(cur, 1 / rate)
            if n:
                log("info", f"Старата история е преизчислена от {cur} в евро ({n} реда).")
    if rates.unknown:
        log("warning", f"Непознати валути (цените им не са превърнати в евро): {', '.join(sorted(rates.unknown))}")

    # Изгодни комбинации -> Slack
    deals = find_deals(db, config, today_s)
    if deals:
        text, blocks = deals_message(deals, dashboard_url)
        sent = slack.send(text, blocks) if notify else False
        for d in deals:
            for item in d["trips"]:
                t = item["trip"]
                log("notice", f"🔔 {d['name']}: {fmt_eur(t.total)} {fmt_day(t.out.date)} → "
                              f"{fmt_day(t.back.date)} – {item['verdict'].label}")
                if sent:
                    db.remember_alert(item["alert_key"], t.total)
        if notify and not sent:
            log("warning", "Известието за изгодни комбинации не беше изпратено.")

    # Обобщение по маршрут (лог + Actions summary)
    rows = []
    for route in routes:
        out = cheapest_per_date(current_legs(db, route.airlines, route.origin, route.destination, today_s))
        back = cheapest_per_date(current_legs(db, route.airlines, route.destination, route.origin, today_s))
        trips = build_trips(out, back, route.nights, "long_weekend", today)
        rows.append({"name": route.name, "target": route.target_price,
                     "best": trips[0] if trips else None, "changes": changes_by_route.get(route.key, 0)})
        if trips:
            t = trips[0]
            log("info", f"{route.name}: най-евтин дълъг уикенд {fmt_eur(t.total)} "
                        f"({fmt_day(t.out.date)} → {fmt_day(t.back.date)}, {t.nights} нощ.)")

    db.finish_scan(scan_id, ok=ok_legs, errors=errors)

    if errors and notify and config.settings.notify_on_errors:
        total = sum(len(r.airlines) for r in routes)
        text, blocks = errors_message(errors, total)
        slack.send(text, blocks)

    if summary_path and not dry_run:
        export_summary(db, config, summary_path, rates=rates)
        log("info", f"Записан {summary_path}")
    db.close()

    write_step_summary(rows, errors, notes)
    print(f"\nГотово: {ok_legs} успешни проверки, {len(errors)} с грешка, {calls} заявки.")
    # Червено в Actions само ако нищо не е минало
    return 1 if not ok_legs else 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Следене на цените на полети")
    ap.add_argument("command", nargs="?", default="scan", choices=["scan", "export"])
    ap.add_argument("--config", default=str(ROOT / "flights.yaml"))
    ap.add_argument("--db", default=str(ROOT / "data" / "flights.db"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "data" / "summary.json"),
                    help="къде да се запише summary.json за сайта")
    ap.add_argument("--dry-run", action="store_true", help="без запис в базата и без Slack")
    ap.add_argument("--no-notify", action="store_true", help="без Slack")
    ap.add_argument("--no-export", action="store_true", help="без summary.json")
    args = ap.parse_args(argv)

    if args.command == "export":
        try:
            config = load_config(args.config)
        except ConfigError as e:
            log("error", str(e).replace("\n", " "))
            sys.exit(2)
        db = Database(args.db)
        db.sync_routes(config.routes)
        export_summary(db, config, Path(args.out), rates=Rates.load(db, fetch=False))
        db.close()
        print(f"Записан {args.out}")
        return

    sys.exit(run(Path(args.config), Path(args.db), args.dry_run, not args.no_notify,
                 None if args.no_export else Path(args.out)))


if __name__ == "__main__":
    main()
