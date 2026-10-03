"""Цялостен тест на проверката с измислени авиокомпании (без мрежа)."""
import json
from datetime import date, timedelta

import pytest

from scanner import main as main_mod
from scanner.airlines import MonthFares, RouteNotServed
from scanner.db import Database, Fare
from scanner.fx import Rates


class FakeAirline:
    """Цени: отиване = 20 € в четвъртък/петък, 60 € иначе; връщане 25 €."""
    calls = 0

    def __init__(self, key, price_out=20.0, serves=True):
        self.key, self.name = key, key.title()
        self.price_out, self.serves = price_out, serves
        self.currency_seen = "EUR"

    def scan_month(self, origin, destination, year, month):
        FakeAirline.calls += 1
        if not self.serves:
            raise RouteNotServed(f"{self.name} не лети {origin}-{destination}")
        res = MonthFares()
        d = date(year, month, 1)
        while d.month == month:
            iso = d.isoformat()
            p = self.price_out if d.weekday() in (3, 4) else 60.0
            res.outbound[iso] = Fare(p, "EUR", p, "06:40", "09:10")
            if d.weekday() in (0, 1, 5):
                res.inbound[iso] = Fare(25.0, "EUR", 25.0, "10:00")
            d += timedelta(days=1)
        return res


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "flights.yaml").write_text("""
settings:
  airlines: [ryanair, wizzair]
  months_ahead: 2
  patterns: [long_weekend]
  nights: [3, 4]
  request_delay: 0
routes:
  - to: BCN
    name: Барселона
    target_price: 50
  - to: XYZ
    name: Никъде
    target_price: 10
    airlines: [wizzair]
""", encoding="utf-8")
    fakes = {"ryanair": FakeAirline("ryanair", 20.0), "wizzair": FakeAirline("wizzair", 18.0, serves=False)}
    monkeypatch.setattr(main_mod, "airline_for", lambda key, fetcher, rates, log=print: fakes[key])
    monkeypatch.setattr(main_mod.Rates, "load", classmethod(lambda cls, db=None, fetch=True, log=print: Rates({}, "test")))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    sent = []
    monkeypatch.setattr(main_mod.SlackNotifier, "mode", property(lambda self: "bot"))
    monkeypatch.setattr(main_mod.SlackNotifier, "send", lambda self, text, blocks=None: sent.append((text, blocks)) or True)
    return tmp_path, sent


def test_full_run(project):
    tmp, sent = project
    db_path, out = tmp / "flights.db", tmp / "summary.json"
    code = main_mod.run(tmp / "flights.yaml", db_path, dry_run=False, notify=True, summary_path=out)
    assert code == 0

    db = Database(db_path)
    scan = db.last_scan()
    assert scan["ok_count"] == 1 and scan["fail_count"] == 0           # Wizz Air „не лети“ – не е грешка
    legs = db.legs()
    assert ("ryanair", "SOF", "BCN") in legs and ("ryanair", "BCN", "SOF") in legs
    assert not any(a == "wizzair" for a, _o, _d in legs)
    as_of, prices = db.current_prices("ryanair", "SOF", "BCN", scan["scan_date"])
    assert as_of == scan["scan_date"] and len(prices) > 20

    # известие: пт -> пн за 45 € е под целта 50 €
    assert len(sent) == 1
    text, blocks = sent[0]
    assert "под целта" in text
    assert "Барселона" in json.dumps(blocks, ensure_ascii=False)
    assert db.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] >= 1

    # summary.json за сайта
    data = json.loads(out.read_text(encoding="utf-8"))
    route = next(r for r in data["routes"] if r["key"] == "SOF-BCN")
    assert route["target_price"] == 50 and len(route["legs"]) == 2
    leg = next(l for l in route["legs"] if l["origin"] == "SOF")
    assert leg["as_of"] == scan["scan_date"] and len(leg["fares"]) > 20
    assert all(len(v) == 1 for v in leg["history"].values())
    db.close()

    # Втора проверка същия ден: нищо ново, без второ известие
    code = main_mod.run(tmp / "flights.yaml", db_path, dry_run=False, notify=True, summary_path=None)
    assert code == 0 and len(sent) == 1
    db = Database(db_path)
    assert db.conn.execute("SELECT COUNT(*) FROM fares WHERE price IS NULL").fetchone()[0] == 0
    db.close()
