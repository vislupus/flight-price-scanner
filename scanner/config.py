"""Зареждане и проверка на flights.yaml."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

AIRLINES = ("ryanair", "wizzair")
PATTERNS = ("long_weekend", "workweek", "any")
IATA = re.compile(r"^[A-Z]{3}$")


class ConfigError(Exception):
    pass


@dataclass
class RouteConfig:
    origin: str
    destination: str
    name: str
    airlines: list[str]
    nights: list[int]
    target_price: float | None = None
    active: bool = True

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.destination}"


@dataclass
class Settings:
    home: str = "SOF"
    airlines: list[str] = field(default_factory=lambda: list(AIRLINES))
    months_ahead: int = 12
    patterns: list[str] = field(default_factory=lambda: ["long_weekend"])
    nights: list[int] = field(default_factory=lambda: [3, 4])
    repeat_alerts_daily: bool = False
    notify_on_errors: bool = True
    alerts_per_route: int = 3
    request_delay: float = 1.5


@dataclass
class Config:
    settings: Settings
    routes: list[RouteConfig] = field(default_factory=list)

    @property
    def active_routes(self) -> list[RouteConfig]:
        return [r for r in self.routes if r.active]


def _iata(value, where: str, errors: list[str]) -> str | None:
    code = str(value or "").strip().upper()
    if not IATA.match(code):
        errors.append(f"{where}: '{value}' не е валиден IATA код (3 латински букви).")
        return None
    return code


def _airlines(value, where: str, errors: list[str]) -> list[str] | None:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        errors.append(f"{where}: airlines трябва да е списък, напр. [ryanair, wizzair].")
        return None
    out = []
    for a in value:
        a = str(a).strip().lower()
        if a not in AIRLINES:
            errors.append(f"{where}: непозната авиокомпания '{a}'. Поддържат се: {', '.join(AIRLINES)}.")
            return None
        if a not in out:
            out.append(a)
    return out


def _nights(value, where: str, errors: list[str]) -> list[int] | None:
    if isinstance(value, int):
        value = [value]
    if not isinstance(value, list) or not value:
        errors.append(f"{where}: nights трябва да е списък от числа, напр. [3, 4].")
        return None
    out = []
    for n in value:
        try:
            n = int(n)
        except (TypeError, ValueError):
            errors.append(f"{where}: nights съдържа '{n}', което не е число.")
            return None
        if not 1 <= n <= 30:
            errors.append(f"{where}: nights трябва да е между 1 и 30.")
            return None
        if n not in out:
            out.append(n)
    return sorted(out)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Файлът {path} не съществува.")
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    errors: list[str] = []
    s = raw.get("settings") or {}
    settings = Settings()
    settings.home = _iata(s.get("home", "SOF"), "settings.home", errors) or "SOF"
    settings.airlines = _airlines(s.get("airlines", list(AIRLINES)), "settings", errors) or list(AIRLINES)
    settings.nights = _nights(s.get("nights", [3, 4]), "settings", errors) or [3, 4]
    try:
        settings.months_ahead = int(s.get("months_ahead", 12))
        if not 1 <= settings.months_ahead <= 13:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("settings.months_ahead трябва да е число между 1 и 13.")
    patterns = s.get("patterns", ["long_weekend"])
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or not patterns:
        errors.append("settings.patterns трябва да е списък.")
    else:
        bad = [p for p in patterns if p not in PATTERNS]
        if bad:
            errors.append(f"settings.patterns: непознати стойности {bad}. Поддържат се: {', '.join(PATTERNS)}.")
        else:
            settings.patterns = list(dict.fromkeys(patterns))
    settings.repeat_alerts_daily = bool(s.get("repeat_alerts_daily", False))
    settings.notify_on_errors = bool(s.get("notify_on_errors", True))
    settings.alerts_per_route = max(1, int(s.get("alerts_per_route", 3) or 3))
    settings.request_delay = float(s.get("request_delay", 1.5) or 0)

    items = raw.get("routes")
    if not isinstance(items, list) or not items:
        raise ConfigError("В flights.yaml няма списък 'routes'.")

    routes: list[RouteConfig] = []
    seen: dict[str, int] = {}
    for i, item in enumerate(items, start=1):
        where = f"маршрут #{i}"
        if not isinstance(item, dict):
            errors.append(f"{where}: очаква се обект с 'to'.")
            continue
        dest = _iata(item.get("to"), f"{where} (to)", errors)
        origin = _iata(item.get("from", settings.home), f"{where} (from)", errors)
        if not dest or not origin:
            continue
        if dest == origin:
            errors.append(f"{where}: 'from' и 'to' са еднакви ({dest}).")
            continue
        airlines = (_airlines(item["airlines"], where, errors)
                    if item.get("airlines") is not None else list(settings.airlines))
        nights = (_nights(item["nights"], where, errors)
                  if item.get("nights") is not None else list(settings.nights))
        if airlines is None or nights is None:
            continue
        target = item.get("target_price")
        if target is not None:
            try:
                target = float(target)
            except (TypeError, ValueError):
                errors.append(f"{where} ({origin}-{dest}): target_price трябва да е число, а е '{target}'.")
                continue
            if target <= 0:
                errors.append(f"{where} ({origin}-{dest}): target_price трябва да е > 0.")
                continue
        key = f"{origin}-{dest}"
        if key in seen:
            errors.append(f"{where}: маршрутът {key} вече е описан в маршрут #{seen[key]}.")
            continue
        seen[key] = i
        routes.append(RouteConfig(
            origin=origin, destination=dest,
            name=str(item.get("name") or dest).strip(),
            airlines=airlines, nights=nights, target_price=target,
            active=bool(item.get("active", True)),
        ))

    if errors:
        raise ConfigError("Грешки във flights.yaml:\n  - " + "\n  - ".join(errors))
    return Config(settings=settings, routes=routes)
