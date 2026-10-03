import pytest

from scanner.config import ConfigError, load_config


def test_load_config(tmp_path):
    p = tmp_path / "flights.yaml"
    p.write_text("""
settings:
  home: sof
  airlines: [ryanair]
  nights: [3, 4]
  patterns: [long_weekend]
routes:
  - to: bcn
    name: Барселона
    target_price: 60
    airlines: [ryanair, wizzair]
  - to: LTN
    from: VAR
    nights: 5
    active: false
""", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.settings.home == "SOF" and cfg.settings.airlines == ["ryanair"]
    r1, r2 = cfg.routes
    assert r1.key == "SOF-BCN" and r1.airlines == ["ryanair", "wizzair"] and r1.target_price == 60
    assert r2.key == "VAR-LTN" and r2.nights == [5] and r2.airlines == ["ryanair"] and not r2.active
    assert [r.key for r in cfg.active_routes] == ["SOF-BCN"]


def test_config_errors(tmp_path):
    p = tmp_path / "flights.yaml"
    p.write_text("routes:\n  - to: B\n  - to: BCN\n    target_price: -5\n  - to: SOF\n", encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    msg = str(e.value)
    assert "IATA" in msg and "target_price" in msg and "еднакви" in msg
