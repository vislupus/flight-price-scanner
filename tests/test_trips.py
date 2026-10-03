from datetime import date

from scanner.trips import Leg, build_trips, cheapest_per_date, fmt_day, matches


def legs(airline, prices):
    return {d: Leg(airline, d, p) for d, p in prices.items()}


def test_patterns():
    thu, fri, sat, mon, tue = (date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 7),
                               date(2026, 11, 9), date(2026, 11, 10))
    assert matches("long_weekend", thu, mon) and matches("long_weekend", fri, tue)
    assert not matches("long_weekend", sat, mon)
    assert matches("workweek", mon, fri := date(2026, 11, 13))
    assert not matches("workweek", sat, mon)
    assert matches("any", sat, mon)


def test_build_trips_mixes_airlines_and_sorts():
    out = cheapest_per_date({
        "ryanair": legs("ryanair", {"2026-11-05": 40, "2026-11-06": 30}),
        "wizzair": legs("wizzair", {"2026-11-05": 35}),
    })
    back = cheapest_per_date({
        "ryanair": legs("ryanair", {"2026-11-09": 25, "2026-11-10": 50}),
        "wizzair": legs("wizzair", {"2026-11-09": 20}),
    })
    assert out["2026-11-05"].airline == "wizzair" and back["2026-11-09"].airline == "wizzair"
    trips = build_trips(out, back, [3, 4], "long_weekend", date(2026, 10, 1))
    totals = [(t.out.date, t.back.date, t.nights, t.total) for t in trips]
    # пт 06.11 -> пн 09.11 (3 нощ.) = 30 + 20
    assert totals[0] == ("2026-11-06", "2026-11-09", 3, 50.0)
    # чт 05.11 -> пн 09.11 (4 нощ.) = 35 + 20
    assert totals[1] == ("2026-11-05", "2026-11-09", 4, 55.0)
    # пт 06.11 -> вт 10.11 (4 нощ.) = 30 + 50
    assert totals[2] == ("2026-11-06", "2026-11-10", 4, 80.0)
    assert len(trips) == 3
    assert build_trips(out, back, [3, 4], "long_weekend", date(2026, 11, 7)) == []


def test_fmt_day():
    assert fmt_day("2026-11-06") == "пт 06.11"
