"""Еднократен внос на старата история от стария скрипт в SQLite.

Старият скрипт пазеше две неща:
  * data/legacy/*.csv – по един файл на посока и месец (SOF-BCN_11-2024.csv):
    ред = дата на проверка, колони = дните от месеца, стойност = най-евтината
    цена на Ryanair за този ден. Без валута и без часове.
  * json/<дд-мм-гггг> - SOF-BCN_11-2024.json – суровите отговори на Ryanair,
    с валута, часове на полета и „разпродаден“. Те са ~500 MB и не са в git.

Двата източника се сливат по дата на проверка (JSON има предимство, защото
е по-пълен), а после се превръщат в интервали (виж scanner/db.py) с legacy=1.

    python scripts/import_legacy.py                         # само CSV от data/legacy
    python scripts/import_legacy.py --json /път/до/json     # CSV + JSON
    python scripts/import_legacy.py --csv папка --db data/flights.db

Може да се пусне повторно – вече внесените редове се прескачат.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scanner.db import Database  # noqa: E402

CSV_RE = re.compile(r"^([A-Z]{3})-([A-Z]{3})_(\d{2})-(\d{4})\.csv$")
JSON_RE = re.compile(r"^(\d{2}-\d{2}-\d{4}) - ([A-Z]{3})-([A-Z]{3})_(\d{2})-(\d{4})\.json$")

# {(origin, dest): {flight_date: {scan_date: (price, currency, dep, arr)}}}
Points = dict[tuple[str, str], dict[str, dict[str, tuple]]]


def _hhmm(value: str | None) -> str | None:
    return value.split("T", 1)[1][:5] if value and "T" in value else None


def read_csv(path: Path, points: Points) -> int:
    """Чете един CSV; не презаписва точки, които вече са дошли от JSON."""
    m = CSV_RE.match(path.name)
    origin, dest, month, year = m.groups()
    leg = points.setdefault((origin, dest), {})
    n = 0
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader, None)
        if not header or header[0] != "Date":
            raise ValueError("липсва заглавен ред")
        days = []
        for col in header[1:]:
            dm = re.match(r"Day (\d{2})", col)
            days.append(f"{year}-{month}-{dm.group(1)}" if dm else None)
        for row in reader:
            if not row or not row[0].strip():
                continue
            try:
                scan = datetime.strptime(row[0].strip(), "%d-%m-%Y").date().isoformat()
            except ValueError:
                continue
            for fd, cell in zip(days, row[1:]):
                cell = cell.strip()
                if not fd or not cell:
                    continue
                try:
                    price = float(cell)
                except ValueError:
                    continue
                leg.setdefault(fd, {}).setdefault(scan, (price, "?", None, None))
                n += 1
    return n


def read_json(path: Path, points: Points) -> int:
    m = JSON_RE.match(path.name)
    scan = datetime.strptime(m.group(1), "%d-%m-%Y").date().isoformat()
    origin, dest = m.group(2), m.group(3)
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    n = 0
    for side, (o, d) in (("outbound", (origin, dest)), ("inbound", (dest, origin))):
        leg = points.setdefault((o, d), {})
        for item in (data.get(side) or {}).get("fares") or []:
            fd = item.get("day")
            price = (item.get("price") or {}).get("value")
            if not fd:
                continue
            if price is None:
                if item.get("soldOut"):
                    leg.setdefault(fd, {})[scan] = (None, None, None, None)
                continue
            leg.setdefault(fd, {})[scan] = (
                float(price), (item.get("price") or {}).get("currencyCode") or "?",
                _hhmm(item.get("departureDate")), _hhmm(item.get("arrivalDate")),
            )
            n += 1
    return n


def to_intervals(series: dict[str, tuple]) -> list[tuple]:
    """{дата на проверка: (price, currency, dep, arr)} -> [(first, last, price, currency, dep, arr)]
    за еднакви поредни цени."""
    out: list[list] = []
    for scan in sorted(series):
        price, currency, dep, arr = series[scan]
        if out and out[-1][2] == price and (out[-1][3] == currency or "?" in (out[-1][3], currency)):
            out[-1][1] = scan
            if out[-1][3] == "?":          # точка от CSV без валута – взима я от JSON
                out[-1][3] = currency
            if dep and not out[-1][4]:
                out[-1][4], out[-1][5] = dep, arr
        else:
            out.append([scan, scan, price, currency, dep, arr])
    return [tuple(x) for x in out]


def load_points(csv_dir: Path | None, json_dir: Path | None, log=print) -> Points:
    points: Points = {}
    if json_dir:
        files = sorted(p for p in json_dir.rglob("*.json") if JSON_RE.match(p.name))
        n = 0
        for i, p in enumerate(files, 1):
            try:
                n += read_json(p, points)
            except (ValueError, OSError) as e:
                log(f"Пропускам {p.name}: {e}")
            if i % 2000 == 0:
                log(f"  JSON: {i}/{len(files)} файла")
        log(f"JSON: {len(files)} файла, {n} цени")
    if csv_dir:
        files = sorted(p for p in csv_dir.glob("*.csv") if CSV_RE.match(p.name))
        n = 0
        for p in files:
            try:
                n += read_csv(p, points)
            except ValueError as e:
                log(f"Пропускам {p.name}: {e}")
        log(f"CSV: {len(files)} файла, {n} цени (включително дублиращи JSON)")
    return points


def import_points(points: Points, db: Database, airline: str = "ryanair") -> tuple[int, int]:
    added = skipped = 0
    for (origin, dest), by_date in points.items():
        rows = []
        for fd, series in by_date.items():
            for first, last, price, currency, dep, arr in to_intervals(series):
                rows.append((fd, first, last, price, currency, price if currency in ("EUR", "?") else None, dep, arr))
        n = db.insert_intervals(airline, origin, dest, rows, legacy=True)
        added += n
        skipped += len(rows) - n
    # Ако всички внесени цени имат известна валута, няма какво да се разпознава после
    unknown = db.conn.execute("SELECT COUNT(*) FROM fares WHERE legacy = 1 AND currency = '?'").fetchone()[0]
    currencies = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT currency FROM fares WHERE legacy = 1 AND currency IS NOT NULL AND currency != '?'")]
    if not unknown and len(currencies) == 1 and not db.get_meta("legacy_currency"):
        db.set_meta("legacy_currency", currencies[0])
    db.vacuum()
    return added, skipped


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(root / "data" / "legacy"), help="папка със старите CSV")
    ap.add_argument("--json", default=None, help="папка със суровите JSON отговори (по избор)")
    ap.add_argument("--db", default=str(root / "data" / "flights.db"))
    args = ap.parse_args()

    csv_dir = Path(args.csv) if args.csv and Path(args.csv).is_dir() else None
    json_dir = Path(args.json) if args.json else None
    if json_dir and not json_dir.is_dir():
        sys.exit(f"Няма такава папка: {json_dir}")
    if not csv_dir and not json_dir:
        sys.exit("Няма нито CSV, нито JSON за внос.")

    points = load_points(csv_dir, json_dir)
    db = Database(args.db)
    added, skipped = import_points(points, db)
    cur = db.get_meta("legacy_currency")
    db.close()
    print(f"Внесени {added} интервала, пропуснати {skipped} (вече ги има). "
          f"Валута на старата история: {cur or 'непозната – ще се разпознае при първата проверка'}.")


if __name__ == "__main__":
    main()
