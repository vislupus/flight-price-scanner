"""Авиокомпании. Всяка има клас с:

    name            – име за показване
    key             – 'ryanair' | 'wizzair'
    scan_month(origin, destination, year, month) -> MonthFares

MonthFares дава най-евтиния полет за всеки ден в двете посоки.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..db import Fare


@dataclass
class MonthFares:
    outbound: dict[str, Fare] = field(default_factory=dict)   # {flight_date: Fare} origin -> destination
    inbound: dict[str, Fare] = field(default_factory=dict)    # destination -> origin


class RouteNotServed(Exception):
    """Авиокомпанията не лети по този маршрут – няма смисъл да се пробват и другите месеци."""


def airline_for(key: str, fetcher, rates, log=print):
    if key == "ryanair":
        from .ryanair import Ryanair
        return Ryanair(fetcher, rates, log)
    if key == "wizzair":
        from .wizzair import WizzAir
        return WizzAir(fetcher, rates, log)
    raise ValueError(f"Непозната авиокомпания: {key}")


AIRLINE_NAMES = {"ryanair": "Ryanair", "wizzair": "Wizz Air"}
