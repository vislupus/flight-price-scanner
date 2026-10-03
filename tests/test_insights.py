from scanner.insights import (PairContext, lead_time_stats, month_stats, pair_context, pair_series,
                              percentile_of, season_labels, verdict)


def test_pair_series_and_context():
    out = [[("2026-09-01", "2026-09-10", 40.0), ("2026-09-11", "2026-09-20", 30.0)]]
    inb = [[("2026-09-01", "2026-09-15", 20.0), ("2026-09-16", "2026-09-20", 25.0)]]
    assert pair_series(out, inb) == [("2026-09-01", 60.0), ("2026-09-11", 50.0), ("2026-09-16", 55.0)]
    ctx = pair_context(out, inb, "2026-09-20")
    assert ctx.hist_min == 50.0 and ctx.hist_min_on == "2026-09-11"
    assert ctx.ago_7 == 50.0 and ctx.days_tracked == 19


def test_two_airlines_take_cheapest():
    out = [[("2026-09-01", "2026-09-10", 40.0)], [("2026-09-05", "2026-09-10", 35.0)]]
    inb = [[("2026-09-01", "2026-09-10", 20.0)]]
    assert pair_series(out, inb) == [("2026-09-01", 60.0), ("2026-09-05", 55.0)]


def test_verdict_rules():
    ctx = PairContext(hist_min=50, hist_min_on="2026-09-11", ago_7=55, days_tracked=20)
    v = verdict(50, target=60, ctx=ctx, percentile=0.1, lead_days=40, lead_stats=None)
    assert v.code == "buy" and any("под целта" in r for r in v.reasons)

    ctx = PairContext(hist_min=40, hist_min_on="2026-09-11", ago_7=50, days_tracked=20)
    v = verdict(60, target=None, ctx=ctx, percentile=0.8, lead_days=120,
                lead_stats={"n": 50, "p_drop10": 0.7, "median_drop": 0.3, "p_rise10": 0.1})
    assert v.code == "wait"

    v = verdict(45, target=None, ctx=PairContext(), percentile=0.1, lead_days=40,
                lead_stats={"n": 50, "p_drop10": 0.2, "median_drop": 0.0, "p_rise10": 0.8},
                levels={"low": 50, "typical": 70, "high": 90})
    assert v.code == "buy" and any("историята" in r for r in v.reasons)

    v = verdict(60, target=None, ctx=PairContext(), percentile=None, lead_days=100, lead_stats=None)
    assert v.code == "hold"


def test_lead_time_stats():
    # полет на 2026-03-01, следен от 100 дни преди до деня преди излитане
    hist = {}
    for i in range(8):
        fd = f"2026-03-{i + 1:02d}"
        # цената пада от 80 на 50 около 30 дни преди полета, после се качва на 90
        hist[("ryanair", fd)] = [
            ("2025-11-01", "2026-01-25", 80.0),
            ("2026-02-01", f"2026-02-{20 + i:02d}", 50.0),
            (f"2026-02-{21 + i:02d}", "2026-03-10", 90.0),
        ]
    stats = lead_time_stats(hist, "2026-04-01", cheap_threshold=60)
    by = {(s["lo"], s["hi"]): s for s in stats}
    assert by[(91, 180)]["n"] == 8 and by[(91, 180)]["p_drop10"] == 1.0   # от 80 падна на 50
    assert by[(91, 180)]["cheap"] is None                                  # 80 не е „евтино“
    assert by[(15, 30)]["cheap"]["n"] == 8 and by[(15, 30)]["cheap"]["p_drop10"] == 0.0
    assert by[(1, 7)]["p_drop10"] == 0.0                                    # накрая само расте
    assert by[(1, 7)]["p_rise10"] == 0.0 or by[(1, 7)]["median_change"] >= 0


def test_month_stats_and_seasons():
    out = {"2026-01-05": [("2025-10-01", "2025-10-05", 30.0), ("2025-10-06", "2025-10-07", 20.0)],
           "2026-07-05": [("2025-10-01", "2025-10-05", 90.0)]}
    inb = {"2026-01-08": [("2025-10-01", "2025-10-05", 25.0)],
           "2026-07-08": [("2025-10-01", "2025-10-05", 95.0)]}
    rows = month_stats(out, inb)
    assert [r["month"] for r in rows] == ["2026-01", "2026-07"]
    assert rows[0]["total"] == 45.0 and rows[1]["total"] == 185.0
    assert season_labels(rows) == {"01": "cheap", "07": "expensive"}
    assert percentile_of(5, [1, 2, 5, 10]) == 0.5
