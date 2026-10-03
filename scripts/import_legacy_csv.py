"""Еднократен внос на старата история (data/legacy/*.csv) в SQLite.

Старият скрипт пишеше по един CSV на посока и месец (SOF-BCN_11-2024.csv):
ред = дата на проверка, колони = дните от месеца, стойност = най-евтината
цена на Ryanair за този ден. Тук се превръщат в интервали (виж scanner/db.py)
със source='legacy'.

Старият скрипт не пазеше валутата. Редовете влизат с currency='?' и
price_eur = price; при първата истинска проверка скенерът научава в каква
валута отговаря Ryanair и ги преизчислява (scanner/main.py).

    python scripts/import_legacy_csv.py                 # data/legacy -> data/flights.db
    python scripts/import_legacy_csv.py папка --db data/flights.db

Може да се пусне повторно – вече внесените редове се прескачат.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scanner.db import Database  # noqa: E402

NAME_RE = re.compile(r"^([A-Z]{3})-([A-Z]{3})_(\d{2})-(\d{4})\.csv$")


def read_csv(path: Path) -> dict[str, list[tuple[str, float]]]:
    """{дата на полет: [(дата на проверка, цена), ...]} подредени по дата на проверка."""
    m = NAME_RE.match(path.name)
    if not m:
        raise ValueError(f"Непознато име на файл: {path.name}")
    _o, _d, month, year = m.groups()
    rows: dict[str, list[tuple[str, float]]] = {}
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader, None)
        if not header or header[0] != "Date":
            raise ValueError(f"{path.name}: липсва заглавен ред")
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
                rows.setdefault(fd, []).append((scan, price))
    for v in rows.values():
        v.sort()
    return rows


def to_intervals(points: list[tuple[str, float]]) -> list[tuple[str, str, float]]:
    """Поредица (дата, цена) -> [(first_seen, last_seen, price)] за еднакви цени."""
    out: list[list] = []
    for scan, price in points:
        if out and out[-1][2] == price:
            out[-1][1] = scan
        else:
            out.append([scan, scan, price])
    return [tuple(x) for x in out]


def import_dir(folder: Path, db: Database, airline: str = "ryanair") -> tuple[int, int, int]:
    files = sorted(folder.glob("*.csv"))
    added = skipped = 0
    for path in files:
        m = NAME_RE.match(path.name)
        if not m:
            print(f"Пропускам {path.name} (непознато име)")
            continue
        origin, dest = m.group(1), m.group(2)
        try:
            rows = read_csv(path)
        except ValueError as e:
            print(f"Пропускам {path.name}: {e}")
            continue
        batch = []
        for fd, points in rows.items():
            for first, last, price in to_intervals(points):
                batch.append((fd, first, last, price, "?", price))
        n = db.insert_intervals(airline, origin, dest, batch, legacy=True)
        added += n
        skipped += len(batch) - n
    db.vacuum()
    return len(files), added, skipped


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", default=str(root / "data" / "legacy"))
    ap.add_argument("--db", default=str(root / "data" / "flights.db"))
    args = ap.parse_args()

    db = Database(args.db)
    files, added, skipped = import_dir(Path(args.folder), db)
    # Ако вече имаме истинска проверка, старите редове веднага получават валута
    cur = db.get_meta("legacy_currency")
    if cur and added:
        row = db.conn.execute(
            "SELECT price, price_eur FROM fares WHERE legacy = 0 AND currency = ? "
            "AND price IS NOT NULL AND price_eur IS NOT NULL LIMIT 1", (cur,)).fetchone()
        if row and row["price"]:
            db.set_meta("legacy_currency", None)
            db.resolve_legacy_currency(cur, row["price_eur"] / row["price"])
    db.close()
    print(f"{files} файла: внесени {added} интервала, пропуснати {skipped} (вече ги има).")


if __name__ == "__main__":
    main()
