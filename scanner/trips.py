"""Комбинации отиване + връщане („пътувания“).

Чиста логика без база и мрежа – същата е повторена в сайта (docs/index.html),
за да може там периодът и дните да се сменят на място.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# weekday(): понеделник = 0 … неделя = 6
PATTERNS: dict[str, dict] = {
    "long_weekend": {"label": "Дълъг уикенд", "hint": "чт/пт → пн/вт",
                     "out": {3, 4}, "back": {0, 1}},
    "workweek": {"label": "Делнични дни", "hint": "пн–пт → пн–пт",
                 "out": {0, 1, 2, 3, 4}, "back": {0, 1, 2, 3, 4}},
    "any": {"label": "Всички дни", "hint": "без ограничение",
            "out": set(range(7)), "back": set(range(7))},
}
WEEKDAYS_BG = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


@dataclass(frozen=True)
class Leg:
    airline: str
    date: str            # YYYY-MM-DD
    price_eur: float
    price: float | None = None
    currency: str | None = None
    dep_time: str | None = None


@dataclass(frozen=True)
class Trip:
    out: Leg
    back: Leg
    nights: int

    @property
    def total(self) -> float:
        return round(self.out.price_eur + self.back.price_eur, 2)

    @property
    def key(self) -> str:
        return f"{self.out.date}|{self.back.date}"


def cheapest_per_date(legs_by_airline: dict[str, dict[str, Leg]]) -> dict[str, Leg]:
    """{дата: най-евтиният полет за деня измежду авиокомпаниите}."""
    best: dict[str, Leg] = {}
    for legs in legs_by_airline.values():
        for d, leg in legs.items():
            if d not in best or leg.price_eur < best[d].price_eur:
                best[d] = leg
    return best


def matches(pattern: str, out_day: date, back_day: date) -> bool:
    p = PATTERNS[pattern]
    return out_day.weekday() in p["out"] and back_day.weekday() in p["back"]


def build_trips(outbound: dict[str, Leg], inbound: dict[str, Leg], nights: list[int],
                pattern: str = "any", date_from: date | None = None) -> list[Trip]:
    """Всички комбинации с посочените нощувки, отговарящи на шаблона, по цена."""
    trips: list[Trip] = []
    for d, out in outbound.items():
        out_day = date.fromisoformat(d)
        if date_from and out_day < date_from:
            continue
        for n in nights:
            back_day = out_day + timedelta(days=n)
            back = inbound.get(back_day.isoformat())
            if back is None or not matches(pattern, out_day, back_day):
                continue
            trips.append(Trip(out=out, back=back, nights=n))
    trips.sort(key=lambda t: (t.total, t.out.date, t.nights))
    return trips


def fmt_day(d: str) -> str:
    """'2026-11-06' -> 'пт 06.11'"""
    day = date.fromisoformat(d)
    return f"{WEEKDAYS_BG[day.weekday()]} {day.day:02d}.{day.month:02d}"
