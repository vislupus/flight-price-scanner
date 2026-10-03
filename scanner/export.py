"""Пресмята всичко, което сайтът показва, в един JSON (docs/data/summary.json).

Пълната база е голяма (всяка цена на всеки полет за всеки ден), а сайтът има
нужда само от текущите цени, историята на бъдещите полети и няколко
обобщения – те се смятат тук и се публикуват заедно със страницата.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .airlines import AIRLINE_NAMES
from .db import Database, today_sofia
from .insights import LEAD_BUCKETS, lead_time_stats, month_stats, price_levels, season_labels, weekday_stats
from .trips import PATTERNS


def _history(db: Database, airlines: list[str], origin: str, destination: str,
             date_from: str | None = None) -> dict[tuple[str, str], list[tuple]]:
    """{(airline, flight_date): [(first_seen, last_seen, price_eur), ...]}"""
    out: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for a in airlines:
        for fd, first, last, price in db.leg_history(a, origin, destination, date_from):
            out[(a, fd)].append((first, last, price))
    return out


def _by_date(history: dict[tuple[str, str], list[tuple]]) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = defaultdict(list)
    for (_a, fd), intervals in history.items():
        out[fd] += intervals
    return out


def route_insights(db: Database, airlines: list[str], origin: str, destination: str, today: str) -> dict:
    out_hist = _history(db, airlines, origin, destination)
    in_hist = _history(db, airlines, destination, origin)
    out_by, in_by = _by_date(out_hist), _by_date(in_hist)
    months = month_stats(out_by, in_by)
    scans = sorted({f for h in (out_hist, in_hist) for iv in h.values() for f, _l, _p in iv})
    lv_out, lv_in = price_levels(out_by), price_levels(in_by)
    levels = None
    if lv_out and lv_in:
        levels = {k: round(lv_out[k] + lv_in[k], 2) for k in ("low", "typical", "high")}
    return {
        "lead_time": {
            "out": lead_time_stats(out_hist, today, lv_out["typical"] if lv_out else None),
            "in": lead_time_stats(in_hist, today, lv_in["typical"] if lv_in else None),
        },
        "levels": {"out": lv_out, "in": lv_in, "total": levels},
        "months": months,
        "season": season_labels(months),
        "weekdays": {"out": weekday_stats(_by_date(out_hist)), "in": weekday_stats(_by_date(in_hist))},
        "first_scan": scans[0] if scans else None,
        "flights_tracked": len(out_hist) + len(in_hist),
    }


def leg_payload(db: Database, airline: str, origin: str, destination: str, today: str) -> dict | None:
    as_of, rows = db.current_prices(airline, origin, destination, today)
    history = _history(db, [airline], origin, destination, today)
    if not rows and not history:
        return None
    fares = [[d, r["price_eur"], r["dep_time"], r["price"], r["currency"]] for d, r in rows.items()]
    hist = {fd: [[f, l, p] for f, l, p in iv] for (_a, fd), iv in history.items()}
    return {"airline": airline, "origin": origin, "destination": destination,
            "as_of": as_of, "fares": fares, "history": hist}


def export_summary(db: Database, config, out_path: Path, rates=None) -> dict:
    today = today_sofia().isoformat()
    last = db.last_scan()
    routes_cfg = {r.key: r for r in config.routes}
    routes = []
    for row in db.routes():
        cfg = routes_cfg.get(row["key"])
        airlines = cfg.airlines if cfg else json.loads(row["airlines"])
        nights = cfg.nights if cfg else json.loads(row["nights"])
        legs = []
        for a in airlines:
            for o, d in ((row["origin"], row["destination"]), (row["destination"], row["origin"])):
                p = leg_payload(db, a, o, d, today)
                if p:
                    legs.append(p)
        if not legs and not row["active"]:
            continue
        routes.append({
            "key": row["key"], "origin": row["origin"], "destination": row["destination"],
            "name": row["name"], "target_price": row["target_price"], "active": bool(row["active"]),
            "airlines": airlines, "nights": nights,
            "legs": legs,
            "insights": route_insights(db, airlines, row["origin"], row["destination"], today),
        })

    data = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "today": today,
        "settings": {
            "home": config.settings.home,
            "patterns": config.settings.patterns,
            "nights": config.settings.nights,
            "airlines": config.settings.airlines,
        },
        "pattern_labels": {k: {"label": v["label"], "hint": v["hint"]} for k, v in PATTERNS.items()},
        "airline_names": AIRLINE_NAMES,
        "lead_buckets": LEAD_BUCKETS,
        "last_scan": dict(last) if last else None,
        "scan_dates": db.scan_dates(),
        "fx": {"source": rates.source, "as_of": rates.as_of} if rates else None,
        "legacy_currency": db.get_meta("legacy_currency"),
        "routes": routes,
    }
    if data["last_scan"] and data["last_scan"].get("errors"):
        data["last_scan"]["errors"] = json.loads(data["last_scan"]["errors"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return data
