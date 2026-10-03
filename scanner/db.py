"""SQLite хранилище на цените.

Цените се пазят „на интервали“: един ред за всяка цена на даден полет
(авиокомпания + посока + дата на полета), с first_seen/last_seen – първата и
последната дата на проверка, на които е видяна точно тази цена. Така базата
расте само когато цена се промени, а не с всяка проверка, но историята е
пълна: цената на даден ден на проверка е редът, чийто интервал го покрива.

За да е малък файлът (той се пази в git), датите са цели числа (дни от
1970-01-01), а цените – в стотинки. За ръчно разглеждане има изглед
`fares_v` с нормални дати и цени:  SELECT * FROM fares_v LIMIT 10;
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1
SOFIA = ZoneInfo("Europe/Sofia")
EPOCH = date(1970, 1, 1).toordinal()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS routes (
    key           TEXT PRIMARY KEY,           -- 'SOF-BCN'
    origin        TEXT NOT NULL,
    destination   TEXT NOT NULL,
    name          TEXT,
    target_price  REAL,                       -- желана цена отиване+връщане, EUR
    airlines      TEXT NOT NULL,              -- JSON списък
    nights        TEXT NOT NULL,              -- JSON списък
    active        INTEGER NOT NULL DEFAULT 1, -- 0 = махнат от flights.yaml
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legs (                -- една посока при една авиокомпания
    id          INTEGER PRIMARY KEY,
    airline     TEXT NOT NULL,                -- ryanair | wizzair
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    UNIQUE(airline, origin, destination)
);

CREATE TABLE IF NOT EXISTS fares (
    leg         INTEGER NOT NULL REFERENCES legs(id),
    flight_day  INTEGER NOT NULL,             -- дни от 1970-01-01
    first_seen  INTEGER NOT NULL,             -- ден на проверка (София), от който важи
    last_seen   INTEGER NOT NULL,             -- последна проверка, на която е потвърдена
    price       INTEGER,                      -- в стотинки на оригиналната валута; NULL = няма цена
    currency    TEXT,                         -- 'EUR', 'GBP', … ; '?' = стара история без валута
    price_eur   INTEGER,                      -- евроцентове
    dep_min     INTEGER,                      -- излитане, минути от полунощ
    arr_min     INTEGER,
    legacy      INTEGER NOT NULL DEFAULT 0,   -- 1 = внесен от стария CSV
    PRIMARY KEY (leg, flight_day, first_seen)
) WITHOUT ROWID;

CREATE VIEW IF NOT EXISTS fares_v AS
    SELECT l.airline, l.origin, l.destination,
           date(f.flight_day * 86400, 'unixepoch') AS flight_date,
           f.price / 100.0 AS price, f.currency, f.price_eur / 100.0 AS price_eur,
           CASE WHEN f.dep_min IS NULL THEN NULL ELSE printf('%02d:%02d', f.dep_min / 60, f.dep_min % 60) END AS dep_time,
           CASE WHEN f.arr_min IS NULL THEN NULL ELSE printf('%02d:%02d', f.arr_min / 60, f.arr_min % 60) END AS arr_time,
           date(f.first_seen * 86400, 'unixepoch') AS first_seen,
           date(f.last_seen * 86400, 'unixepoch') AS last_seen,
           f.legacy
    FROM fares f JOIN legs l ON l.id = f.leg;

CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,               -- ISO 8601, UTC
    finished_at  TEXT,
    scan_date    TEXT NOT NULL,               -- дата по софийско време
    ok_count     INTEGER NOT NULL DEFAULT 0,  -- успешни проверки (авиокомпания × маршрут)
    fail_count   INTEGER NOT NULL DEFAULT 0,
    errors       TEXT                         -- JSON: [{airline, leg, error}]
);

CREATE TABLE IF NOT EXISTS alerts (
    key        TEXT PRIMARY KEY,              -- 'SOF-BCN|2026-11-06|2026-11-09'
    price_eur  REAL NOT NULL,
    sent_at    TEXT NOT NULL
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_sofia() -> date:
    return datetime.now(SOFIA).date()


def day_num(iso: str) -> int:
    return date.fromisoformat(iso).toordinal() - EPOCH


def iso_day(n: int) -> str:
    return date.fromordinal(n + EPOCH).isoformat()


def _cents(value: float | None) -> int | None:
    return None if value is None else int(round(value * 100))


def _money(cents: int | None) -> float | None:
    return None if cents is None else cents / 100


def _minutes(hhmm: str | None) -> int | None:
    if not hhmm or ":" not in hhmm:
        return None
    h, m = hhmm.split(":")[:2]
    try:
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def _hhmm(minutes: int | None) -> str | None:
    return None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Fare:
    """Най-евтиният полет за един ден в една посока."""
    price: float | None
    currency: str | None = None
    price_eur: float | None = None
    dep_time: str | None = None
    arr_time: str | None = None


@dataclass
class LegStats:
    new: int = 0        # нови цени
    extended: int = 0   # непроменени (удължен интервал)
    gone: int = 0       # изчезнали цени
    priced: int = 0     # дни с цена


def _row(r: sqlite3.Row) -> dict:
    """Ред от fares в удобен вид: ISO дати, цени като числа с десетична част."""
    return {
        "leg": r["leg"], "flight_day": r["flight_day"],
        "flight_date": iso_day(r["flight_day"]),
        "first_seen": iso_day(r["first_seen"]), "last_seen": iso_day(r["last_seen"]),
        "price": _money(r["price"]), "currency": r["currency"], "price_eur": _money(r["price_eur"]),
        "dep_time": _hhmm(r["dep_min"]), "arr_time": _hhmm(r["arr_min"]), "legacy": r["legacy"],
    }


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # Класически журнал – без -wal файлове, за да е един файл в git
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()
        self._legs: dict[tuple[str, str, str], int] = {}

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # ---- meta -----------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    # ---- маршрути -------------------------------------------------------

    def sync_routes(self, routes) -> None:
        """Записва/обновява маршрутите от YAML; липсващите маркира като неактивни."""
        now = utcnow_iso()
        keys = []
        for r in routes:
            keys.append(r.key)
            self.conn.execute(
                """
                INSERT INTO routes(key, origin, destination, name, target_price, airlines,
                                   nights, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    name = excluded.name,
                    target_price = excluded.target_price,
                    airlines = excluded.airlines,
                    nights = excluded.nights,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (r.key, r.origin, r.destination, r.name, r.target_price,
                 json.dumps(r.airlines), json.dumps(r.nights), int(r.active), now, now),
            )
        marks = ",".join("?" * len(keys)) or "''"
        self.conn.execute(
            f"UPDATE routes SET active = 0, updated_at = ? WHERE active = 1 AND key NOT IN ({marks})",
            (now, *keys),
        )
        self.conn.commit()

    def routes(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM routes ORDER BY active DESC, name").fetchall()

    # ---- посоки ---------------------------------------------------------

    def leg_id(self, airline: str, origin: str, destination: str, create: bool = True) -> int | None:
        key = (airline, origin, destination)
        if key in self._legs:
            return self._legs[key]
        row = self.conn.execute(
            "SELECT id FROM legs WHERE airline = ? AND origin = ? AND destination = ?", key).fetchone()
        if row is None:
            if not create:
                return None
            cur = self.conn.execute(
                "INSERT INTO legs(airline, origin, destination) VALUES (?, ?, ?)", key)
            self._legs[key] = cur.lastrowid
        else:
            self._legs[key] = row["id"]
        return self._legs[key]

    def legs(self) -> list[tuple[str, str, str]]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT airline, origin, destination FROM legs ORDER BY origin, destination, airline")]

    # ---- цени -----------------------------------------------------------

    def open_rows(self, airline: str, origin: str, destination: str,
                  date_from: str | None = None, date_to: str | None = None) -> dict[str, dict]:
        """Последният ред за всяка дата на полет (този с най-голям last_seen)."""
        leg = self.leg_id(airline, origin, destination, create=False)
        if leg is None:
            return {}
        where = "leg = ?"
        args: list = [leg]
        if date_from:
            where += " AND flight_day >= ?"
            args.append(day_num(date_from))
        if date_to:
            where += " AND flight_day <= ?"
            args.append(day_num(date_to))
        out: dict[str, dict] = {}
        for r in self.conn.execute(
                f"SELECT * FROM fares WHERE {where} ORDER BY flight_day, last_seen, first_seen", args):
            out[iso_day(r["flight_day"])] = _row(r)   # последният по last_seen печели
        return out

    def record_leg(self, airline: str, origin: str, destination: str, scan_date: str,
                   fares: dict[str, Fare], date_from: str, date_to: str) -> LegStats:
        """Записва резултата от една проверка на една посока.

        fares: {flight_date: Fare} за всички дати с полет в проверения период
        [date_from, date_to]. Дати от периода, които липсват, се броят за
        „няма цена“ – ако дотогава са имали цена, се записва ред с NULL.
        """
        stats = LegStats()
        leg = self.leg_id(airline, origin, destination)
        scan = day_num(scan_date)
        current = self.open_rows(airline, origin, destination, date_from, date_to)
        dates = set(fares) | {d for d, r in current.items() if r["price"] is not None}

        for fd in sorted(dates):
            fare = fares.get(fd) or Fare(price=None)
            if fare.price is not None:
                stats.priced += 1
            row = current.get(fd)
            if row is None:
                if fare.price is None:
                    continue  # никога не е имало цена – не записваме нищо
                self._insert(leg, fd, fare, scan)
                stats.new += 1
                continue

            same = row["price"] == fare.price and (fare.price is None or row["currency"] == fare.currency)
            ident = (leg, row["flight_day"], day_num(row["first_seen"]))
            if same:
                if row["last_seen"] < scan_date:
                    self.conn.execute(
                        "UPDATE fares SET last_seen = ?, dep_min = COALESCE(?, dep_min), "
                        "arr_min = COALESCE(?, arr_min), price_eur = COALESCE(?, price_eur) "
                        "WHERE leg = ? AND flight_day = ? AND first_seen = ?",
                        (scan, _minutes(fare.dep_time), _minutes(fare.arr_time), _cents(fare.price_eur), *ident),
                    )
                stats.extended += 1
            elif row["first_seen"] == scan_date:
                # Повторна проверка в същия ден – поправяме реда, вместо да дублираме
                self.conn.execute(
                    "UPDATE fares SET price = ?, currency = ?, price_eur = ?, dep_min = ?, arr_min = ? "
                    "WHERE leg = ? AND flight_day = ? AND first_seen = ?",
                    (_cents(fare.price), fare.currency, _cents(fare.price_eur),
                     _minutes(fare.dep_time), _minutes(fare.arr_time), *ident),
                )
                stats.new += 1
            else:
                self._insert(leg, fd, fare, scan)
                if fare.price is None:
                    stats.gone += 1
                else:
                    stats.new += 1
        self.conn.commit()
        return stats

    def _insert(self, leg: int, fd: str, fare: Fare, scan: int) -> None:
        self.conn.execute(
            """INSERT INTO fares(leg, flight_day, first_seen, last_seen, price, currency,
                                 price_eur, dep_min, arr_min, legacy)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (leg, day_num(fd), scan, scan, _cents(fare.price), fare.currency, _cents(fare.price_eur),
             _minutes(fare.dep_time), _minutes(fare.arr_time)),
        )

    def insert_intervals(self, airline: str, origin: str, destination: str,
                         rows: list[tuple[str, str, str, float, str, float]], legacy: bool = True) -> int:
        """Масов внос: rows = [(flight_date, first_seen, last_seen, price, currency, price_eur)].
        Вече съществуващите (същ полет и first_seen) се прескачат."""
        leg = self.leg_id(airline, origin, destination)
        cur = self.conn.executemany(
            """INSERT OR IGNORE INTO fares(leg, flight_day, first_seen, last_seen, price, currency,
                                           price_eur, legacy)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(leg, day_num(fd), day_num(f), day_num(l), _cents(p), c, _cents(pe), int(legacy))
             for fd, f, l, p, c, pe in rows],
        )
        self.conn.commit()
        return cur.rowcount

    def leg_last_scan(self, airline: str, origin: str, destination: str) -> str | None:
        leg = self.leg_id(airline, origin, destination, create=False)
        if leg is None:
            return None
        row = self.conn.execute("SELECT MAX(last_seen) AS d FROM fares WHERE leg = ?", (leg,)).fetchone()
        return iso_day(row["d"]) if row and row["d"] is not None else None

    def current_prices(self, airline: str, origin: str, destination: str,
                       date_from: str) -> tuple[str | None, dict[str, dict]]:
        """(дата на последната проверка, {flight_date: ред}) – само датите с цена,
        потвърдени при последната проверка на тази посока."""
        as_of = self.leg_last_scan(airline, origin, destination)
        if not as_of:
            return None, {}
        rows = self.open_rows(airline, origin, destination, date_from)
        return as_of, {d: r for d, r in rows.items() if r["last_seen"] == as_of and r["price_eur"] is not None}

    def leg_history(self, airline: str, origin: str, destination: str,
                    date_from: str | None = None) -> list[tuple[str, str, str, float | None]]:
        """[(flight_date, first_seen, last_seen, price_eur), ...] подредени по полет и дата."""
        leg = self.leg_id(airline, origin, destination, create=False)
        if leg is None:
            return []
        where, args = "leg = ?", [leg]
        if date_from:
            where += " AND flight_day >= ?"
            args.append(day_num(date_from))
        return [(iso_day(r["flight_day"]), iso_day(r["first_seen"]), iso_day(r["last_seen"]), _money(r["price_eur"]))
                for r in self.conn.execute(
                    f"SELECT flight_day, first_seen, last_seen, price_eur FROM fares WHERE {where} "
                    f"ORDER BY flight_day, first_seen", args)]

    def flight_intervals(self, airline: str, origin: str, destination: str,
                         flight_date: str) -> list[tuple[str, str, float | None]]:
        leg = self.leg_id(airline, origin, destination, create=False)
        if leg is None:
            return []
        return [(iso_day(r["first_seen"]), iso_day(r["last_seen"]), _money(r["price_eur"]))
                for r in self.conn.execute(
                    "SELECT first_seen, last_seen, price_eur FROM fares WHERE leg = ? AND flight_day = ? "
                    "ORDER BY first_seen", (leg, day_num(flight_date)))]

    # ---- стара история ------------------------------------------------

    def resolve_legacy_currency(self, currency: str, rate_to_eur: float) -> int:
        """Старият CSV не пазеше валута. Когато първата истинска проверка покаже
        в каква валута отговаря Ryanair, преизчисляваме внесените редове."""
        if self.get_meta("legacy_currency"):
            return 0
        cur = self.conn.execute(
            "UPDATE fares SET currency = ?, price_eur = CAST(ROUND(price * ?) AS INTEGER) "
            "WHERE legacy = 1 AND currency = '?' AND price IS NOT NULL",
            (currency, rate_to_eur),
        )
        self.conn.execute("UPDATE fares SET currency = ? WHERE legacy = 1 AND currency = '?'", (currency,))
        self.set_meta("legacy_currency", currency)
        return cur.rowcount

    # ---- известия -------------------------------------------------------

    def last_alert_price(self, key: str) -> float | None:
        row = self.conn.execute("SELECT price_eur FROM alerts WHERE key = ?", (key,)).fetchone()
        return row["price_eur"] if row else None

    def remember_alert(self, key: str, price_eur: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO alerts(key, price_eur, sent_at) VALUES (?, ?, ?)",
            (key, price_eur, utcnow_iso()),
        )
        self.conn.commit()

    # ---- проверки -------------------------------------------------------

    def start_scan(self, scan_date: str) -> int:
        cur = self.conn.execute("INSERT INTO scans(started_at, scan_date) VALUES (?, ?)",
                                (utcnow_iso(), scan_date))
        self.conn.commit()
        return cur.lastrowid

    def finish_scan(self, scan_id: int, ok: int, errors: list[dict]) -> None:
        self.conn.execute(
            "UPDATE scans SET finished_at = ?, ok_count = ?, fail_count = ?, errors = ? WHERE id = ?",
            (utcnow_iso(), ok, len(errors), json.dumps(errors, ensure_ascii=False), scan_id),
        )
        self.conn.commit()

    def last_scan(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scans WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()

    def scan_dates(self) -> list[str]:
        return [r["d"] for r in self.conn.execute(
            "SELECT DISTINCT scan_date AS d FROM scans WHERE finished_at IS NOT NULL ORDER BY 1")]

    def vacuum(self) -> None:
        self.conn.commit()
        self.conn.execute("VACUUM")
