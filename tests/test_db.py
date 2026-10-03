from scanner.db import Database, Fare


def leg(db, day, fares):
    return db.record_leg("ryanair", "SOF", "BCN", day, fares, "2026-11-01", "2026-11-30")


def test_intervals_lifecycle():
    db = Database(":memory:")
    s = leg(db, "2026-10-01", {"2026-11-05": Fare(37.99, "EUR", 37.99, "06:40")})
    assert (s.new, s.extended, s.gone) == (1, 0, 0)

    # същата цена на следващия ден -> интервалът се удължава
    s = leg(db, "2026-10-02", {"2026-11-05": Fare(37.99, "EUR", 37.99, "06:40")})
    assert (s.new, s.extended) == (0, 1)
    rows = db.conn.execute("SELECT * FROM fares_v ORDER BY first_seen").fetchall()
    assert len(rows) == 1 and rows[0]["last_seen"] == "2026-10-02"

    # нова цена -> нов ред
    s = leg(db, "2026-10-03", {"2026-11-05": Fare(29.99, "EUR", 29.99, "06:40")})
    assert s.new == 1
    rows = db.conn.execute("SELECT * FROM fares_v ORDER BY first_seen").fetchall()
    assert len(rows) == 2 and rows[1]["first_seen"] == "2026-10-03"

    # повторна проверка в същия ден с друга цена -> редът се поправя, не се дублира
    leg(db, "2026-10-03", {"2026-11-05": Fare(27.99, "EUR", 27.99, "06:40")})
    rows = db.conn.execute("SELECT * FROM fares_v ORDER BY first_seen").fetchall()
    assert len(rows) == 2 and rows[1]["price"] == 27.99

    # цената изчезва (разпродаден) -> ред с NULL
    s = leg(db, "2026-10-04", {})
    assert s.gone == 1
    rows = db.conn.execute("SELECT * FROM fares_v ORDER BY first_seen").fetchall()
    assert len(rows) == 3 and rows[2]["price"] is None

    # ден, който никога не е имал цена, не се записва
    s = leg(db, "2026-10-05", {"2026-11-20": Fare(None)})
    assert (s.new, s.gone) == (0, 0)
    assert db.conn.execute("SELECT COUNT(*) FROM fares").fetchone()[0] == 3

    # текущи цени: само потвърдените при последната проверка и с цена
    as_of, cur = db.current_prices("ryanair", "SOF", "BCN", "2026-11-01")
    assert as_of == "2026-10-04" and cur == {}
    leg(db, "2026-10-06", {"2026-11-05": Fare(31.99, "EUR", 31.99)})
    as_of, cur = db.current_prices("ryanair", "SOF", "BCN", "2026-11-01")
    assert as_of == "2026-10-06" and list(cur) == ["2026-11-05"] and cur["2026-11-05"]["price"] == 31.99


def test_alerts_and_scans():
    db = Database(":memory:")
    assert db.last_alert_price("x") is None
    db.remember_alert("x", 55.5)
    assert db.last_alert_price("x") == 55.5
    sid = db.start_scan("2026-10-01")
    db.finish_scan(sid, ok=3, errors=[{"airline": "wizzair", "leg": "SOF-BCN", "error": "x"}])
    assert db.last_scan()["fail_count"] == 1
    assert db.scan_dates() == ["2026-10-01"]
