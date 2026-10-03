import pytest

from scanner.fx import Rates


@pytest.fixture
def rates():
    return Rates({"GBP": 0.8, "PLN": 4.0}, "test", "2026-10-01")
