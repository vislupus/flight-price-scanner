import json
from pathlib import Path

from scanner.airlines.ryanair import parse_day_fares
from scanner.airlines.wizzair import parse_flights

FIX = Path(__file__).parent / "fixtures"


def test_ryanair_parser(rates):
    data = json.loads((FIX / "ryanair_month.json").read_text(encoding="utf-8"))
    out = parse_day_fares(data["outbound"], rates)
    assert "2026-11-01" not in out                     # няма полет
    assert out["2026-11-05"].price == 37.99
    assert out["2026-11-05"].price_eur == 37.99
    assert out["2026-11-05"].dep_time == "06:40" and out["2026-11-05"].arr_time == "09:10"
    assert out["2026-11-08"].price is None             # разпродаден
    inb = parse_day_fares(data["inbound"], rates)
    assert inb["2026-11-09"].price == 24.99


def test_wizzair_parser(rates):
    data = json.loads((FIX / "wizzair_month.json").read_text(encoding="utf-8"))
    out = parse_flights(data["outboundFlights"], rates)
    assert out["2026-11-05"].price == 39.99 and out["2026-11-05"].dep_time == "18:45"
    assert out["2026-11-06"].price is None
    inb = parse_flights(data["returnFlights"], rates)
    assert inb["2026-11-09"].currency == "GBP"
    assert inb["2026-11-09"].price_eur == round(34.99 / 0.8, 2)
