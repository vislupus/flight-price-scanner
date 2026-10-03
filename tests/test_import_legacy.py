import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import import_legacy  # noqa: E402

from scanner.db import Database


def test_import_merges_json_and_csv(tmp_path):
    csv_dir, json_dir = tmp_path / "csv", tmp_path / "json"
    csv_dir.mkdir(); json_dir.mkdir()
    (csv_dir / "SOF-BCN_11-2025.csv").write_text(
        "Date;Day 01;Day 02;Day 03\n"
        "01-10-2025;30.5;;40\n"
        "02-10-2025;30.5;;45\n"
        "03-10-2025;28;;45\n", encoding="utf-8")
    (csv_dir / "BCN-SOF_11-2025.csv").write_text(
        "Date;Day 01;Day 02;Day 03\n01-10-2025;;20;\n", encoding="utf-8")
    # JSON за 02-10-2025 носи валута и часове и има предимство пред CSV за същия ден
    fare = lambda day, value, dep: {"day": day, "departureDate": f"{day}T{dep}:00", "arrivalDate": f"{day}T09:00:00",
                                    "price": None if value is None else {"value": value, "currencyCode": "EUR"},
                                    "soldOut": value is None, "unavailable": False}
    (json_dir / "02-10-2025 - SOF-BCN_11-2025.json").write_text(json.dumps({
        "outbound": {"fares": [fare("2025-11-01", 30.5, "06:40"), fare("2025-11-03", 45, "18:00")]},
        "inbound": {"fares": [fare("2025-11-02", None, "10:00")]},
    }), encoding="utf-8")

    points = import_legacy.load_points(csv_dir, json_dir, log=lambda m: None)
    db = Database(":memory:")
    added, skipped = import_legacy.import_points(points, db)
    assert skipped == 0
    rows = db.conn.execute("SELECT * FROM fares_v WHERE origin='SOF' ORDER BY flight_date, first_seen").fetchall()
    r = [dict(x) for x in rows]
    # 01.11: 30.5 от CSV (без валута) на 01.10 и 30.5 EUR от JSON на 02.10 са един интервал
    # (валутата и часът идват от JSON); на 03.10 цената е 28 – нов интервал
    d1 = [x for x in r if x["flight_date"] == "2025-11-01"]
    assert [(x["first_seen"], x["last_seen"], x["price"], x["currency"], x["dep_time"]) for x in d1] == [
        ("2025-10-01", "2025-10-02", 30.5, "EUR", "06:40"), ("2025-10-03", "2025-10-03", 28.0, "?", None)]
    d3 = [x for x in r if x["flight_date"] == "2025-11-03"]
    assert [(x["first_seen"], x["last_seen"], x["price"]) for x in d3] == [
        ("2025-10-01", "2025-10-01", 40.0), ("2025-10-02", "2025-10-03", 45.0)]
    back = db.conn.execute("SELECT * FROM fares_v WHERE origin='BCN' ORDER BY first_seen").fetchall()
    assert [(x["first_seen"], x["price"]) for x in back] == [("2025-10-01", 20.0), ("2025-10-02", None)]
    assert db.get_meta("legacy_currency") is None          # има редове с непозната валута
    assert all(x["legacy"] == 1 for x in r)

    # повторен внос не дублира
    added2, skipped2 = import_legacy.import_points(points, db)
    assert added2 == 0 and skipped2 == added


def test_json_only_sets_currency(tmp_path):
    json_dir = tmp_path / "json"; json_dir.mkdir()
    (json_dir / "02-10-2025 - SOF-BCN_11-2025.json").write_text(json.dumps({
        "outbound": {"fares": [{"day": "2025-11-01", "price": {"value": 30.5, "currencyCode": "EUR"}}]},
        "inbound": {"fares": []}}), encoding="utf-8")
    db = Database(":memory:")
    import_legacy.import_points(import_legacy.load_points(None, json_dir, log=lambda m: None), db)
    assert db.get_meta("legacy_currency") == "EUR"
    assert db.conn.execute("SELECT price_eur FROM fares_v").fetchone()[0] == 30.5
