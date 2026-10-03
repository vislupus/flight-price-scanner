"""Изводи от историята: било ли е по-евтино, кога е евтино, да купя ли сега.

Работи върху „интервалите“ от базата: за всеки полет (посока + дата) има
списък (first_seen, last_seen, price_eur) – коя цена през кои дни на проверка
е била в сила.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

Interval = tuple[str, str, float | None]   # (first_seen, last_seen, price_eur)

LEAD_BUCKETS = [(1, 7), (8, 14), (15, 30), (31, 60), (61, 90), (91, 180), (181, 400)]


# ---- цена на даден ден на проверка ------------------------------------------
#
# Една посока може да има няколко авиокомпании – затова историята на един
# полет е списък от списъци с интервали (по един за авиокомпания). Цената за
# деня е най-ниската измежду тях.

Sets = list[list[Interval]]


def price_on(intervals: list[Interval], day: str) -> float | None:
    """Цената, която е била в сила на деня на проверка `day` (None = няма данни)."""
    for first, last, price in intervals:
        if first <= day <= last:
            return price
    return None


def price_on_sets(sets: Sets, day: str) -> float | None:
    prices = [p for s in sets if (p := price_on(s, day)) is not None]
    return min(prices) if prices else None


def min_price(intervals: list[Interval]) -> tuple[float | None, str | None]:
    """Най-ниската цена някога за този полет и от коя дата на проверка е."""
    best, when = None, None
    for first, _last, price in intervals:
        if price is not None and (best is None or price < best):
            best, when = price, first
    return best, when


def change_points(*sets: Sets) -> list[str]:
    days = set()
    for group in sets:
        for intervals in group:
            for first, last, _p in intervals:
                days.add(first)
                days.add(last)
    return sorted(days)


def pair_series(out_sets: Sets, in_sets: Sets) -> list[tuple[str, float]]:
    """Как се е движила общата цена (отиване + връщане) по дни на проверка.
    Връща само точките, в които тя се е променила."""
    series: list[tuple[str, float]] = []
    for day in change_points(out_sets, in_sets):
        a, b = price_on_sets(out_sets, day), price_on_sets(in_sets, day)
        if a is None or b is None:
            continue
        total = round(a + b, 2)
        if not series or series[-1][1] != total:
            series.append((day, total))
    return series


def total_on(out_sets: Sets, in_sets: Sets, day: str) -> float | None:
    """Общата цена на даден ден – ако някой от двата полета няма наблюдение, се
    взема последното известно преди този ден (до 3 дни назад)."""
    def lookup(sets):
        d = date.fromisoformat(day)
        for back in range(4):
            p = price_on_sets(sets, (d - timedelta(days=back)).isoformat())
            if p is not None:
                return p
        return None
    a, b = lookup(out_sets), lookup(in_sets)
    return None if a is None or b is None else round(a + b, 2)


@dataclass
class PairContext:
    hist_min: float | None = None       # най-ниска обща цена някога за тези дати
    hist_min_on: str | None = None
    ago_7: float | None = None          # обща цена преди 7 дни
    ago_30: float | None = None
    first_seen: str | None = None       # откога се следи тази двойка
    days_tracked: int = 0


def pair_context(out_sets: Sets, in_sets: Sets, today: str) -> PairContext:
    series = pair_series(out_sets, in_sets)
    ctx = PairContext()
    if not series:
        return ctx
    ctx.first_seen = series[0][0]
    ctx.hist_min, ctx.hist_min_on = min((t, d) for d, t in series)
    t = date.fromisoformat(today)
    ctx.days_tracked = max(0, (t - date.fromisoformat(ctx.first_seen)).days)
    ctx.ago_7 = total_on(out_sets, in_sets, (t - timedelta(days=7)).isoformat())
    ctx.ago_30 = total_on(out_sets, in_sets, (t - timedelta(days=30)).isoformat())
    return ctx


# ---- препоръка --------------------------------------------------------------

@dataclass
class Verdict:
    code: str            # buy | good | hold | wait
    label: str
    reasons: list[str] = field(default_factory=list)


VERDICT_LABELS = {
    "buy": "Купи сега",
    "good": "Добра цена",
    "hold": "Нормална цена",
    "wait": "Изчакай",
}


def verdict(total: float, *, target: float | None, ctx: PairContext, percentile: float | None,
            lead_days: int, lead_stats: dict | None, levels: dict | None = None) -> Verdict:
    """Проста, обяснима оценка. Същите правила са и в сайта (docs/index.html).

    percentile – къде е цената спрямо другите възможни пътувания по същия
                 маршрут в момента (0 = най-евтиното, 1 = най-скъпото)
    lead_stats – статистика за този брой дни преди полета, за случаи като
                 този (вече евтино или не): {"n", "p_drop10", "median_drop", "p_rise10"}
    levels     – {"low", "typical", "high"} за отиване + връщане от историята
    """
    reasons: list[str] = []
    score = 0

    if target is not None and total <= target:
        score += 2
        reasons.append(f"под целта ти от {target:.0f} €")

    if levels:
        if total <= levels["low"]:
            score += 1
            reasons.append(f"сред най-ниските цени в историята на маршрута (под {levels['low']:.0f} €)")
        elif total >= levels["high"]:
            score -= 1
            reasons.append(f"над обичайното за маршрута (типично {levels['typical']:.0f} €)")

    if ctx.hist_min is not None and ctx.days_tracked >= 7:
        if total <= ctx.hist_min + 0.01:
            score += 1
            reasons.append(f"най-ниската цена за тези дати, откакто се следят ({ctx.days_tracked} дни)")
        elif total > ctx.hist_min * 1.15:
            score -= 2
            reasons.append(f"за тези дати е било {ctx.hist_min:.0f} € ({ctx.hist_min_on})")
        else:
            reasons.append(f"близо до най-ниската за тези дати ({ctx.hist_min:.0f} €)")

    if ctx.ago_7 is not None and ctx.ago_7 != total:
        d = total - ctx.ago_7
        if d < 0:
            reasons.append(f"поевтиня с {-d:.0f} € за седмица")
        else:
            score -= 1 if d > total * 0.1 else 0
            reasons.append(f"поскъпна с {d:.0f} € за седмица")

    if percentile is not None:
        if percentile <= 0.2:
            score += 1
            reasons.append("сред най-евтините дати по този маршрут")
        elif percentile >= 0.6:
            score -= 1
            reasons.append("има по-евтини дати по този маршрут")

    if lead_stats and lead_stats.get("n", 0) >= 15:
        p_drop, med = lead_stats.get("p_drop10", 0), lead_stats.get("median_drop", 0)
        if p_drop >= 0.6 and med >= 0.15 and lead_days > 21:
            score -= 1
            reasons.append(f"при такава цена {lead_days} дни преди полета е падала още "
                           f"в {p_drop:.0%} от случаите (типично с {med:.0%})")
        elif p_drop <= 0.35:
            score += 1
            reasons.append(f"при такава цена {lead_days} дни преди полета рядко пада още ({p_drop:.0%})")
        else:
            reasons.append(f"{p_drop:.0%} шанс да падне още {lead_days} дни преди полета")
    elif lead_days <= 14:
        score += 1
        reasons.append("остават малко дни – рядко поевтинява в последния момент")

    if score >= 2:
        code = "buy"
    elif score == 1:
        code = "good"
    elif score <= -2:
        code = "wait"
    else:
        code = "hold"
    return Verdict(code, VERDICT_LABELS[code], reasons)


def percentile_of(value: float, values: list[float]) -> float | None:
    if not values:
        return None
    below = sum(1 for v in values if v < value)
    return below / len(values)


# ---- статистики по маршрут ------------------------------------------------------

def lead_bucket(lead_days: int) -> tuple[int, int] | None:
    for lo, hi in LEAD_BUCKETS:
        if lo <= lead_days <= hi:
            return (lo, hi)
    return None


def lead_time_stats(history: dict, today: str, cheap_threshold: float | None = None) -> list[dict]:
    """За всеки период „дни преди полета“: ако купя тогава, колко често цената
    пада още (≥10 %), с колко най-много, и колко често поскъпва накрая.

    history: {(airline, flight_date): интервали} за една посока.
    Ползват се само полети, чиято история стига до ≤ 7 дни преди излитане.
    Отделно („cheap“) се броят само случаите, в които цената в този момент
    вече е била ниска (≤ cheap_threshold) – те отговарят на въпроса „вече е
    евтино, ще падне ли още?“.
    """
    acc: dict[tuple[int, int], list[tuple[float, float, float]]] = defaultdict(list)   # (drop, rise, p0)
    for key, intervals in history.items():
        fd = key[1] if isinstance(key, tuple) else key
        if fd >= today:
            continue
        priced = [(f, l, p) for f, l, p in intervals if p is not None and f < fd]
        if not priced:
            continue
        flight = date.fromisoformat(fd)
        last_obs = max(l for _f, l, _p in priced)
        if (flight - date.fromisoformat(last_obs)).days > 7:
            continue   # историята спира твърде рано – не знаем края
        final_price = next(p for f, l, p in priced if l == last_obs)
        for lo, hi in LEAD_BUCKETS:
            # първото наблюдение в този период
            start = (flight - timedelta(days=hi)).isoformat()
            end = (flight - timedelta(days=lo)).isoformat()
            first = None
            for f, l, p in priced:
                if l >= start and f <= end:
                    first = (max(f, start), p)
                    break
            if first is None:
                continue
            s, p0 = first
            later = [p for f, l, p in priced if l > s]
            if not later:
                continue
            best_later = min(later)
            acc[(lo, hi)].append(((p0 - best_later) / p0, (final_price - p0) / p0, p0))

    def summary(rows):
        drops = [d for d, _r, _p in rows]
        rises = [r for _d, r, _p in rows]
        return {
            "n": len(rows),
            "p_drop10": round(sum(1 for d in drops if d >= 0.10) / len(rows), 3),
            "median_drop": round(statistics.median(drops), 3),
            "p_rise10": round(sum(1 for r in rises if r >= 0.10) / len(rows), 3),
            "median_change": round(statistics.median(rises), 3),
        }

    out = []
    for lo, hi in LEAD_BUCKETS:
        rows = acc.get((lo, hi), [])
        if not rows:
            continue
        item = {"lo": lo, "hi": hi, **summary(rows)}
        if cheap_threshold is not None:
            cheap = [r for r in rows if r[2] <= cheap_threshold]
            item["cheap"] = summary(cheap) if cheap else None
        out.append(item)
    return out


def price_levels(history: dict[str, list[Interval]]) -> dict | None:
    """Нива на цената в една посока от цялата история (по най-ниската видяна
    цена за всяка дата): low = 25-и персентил, typical = медиана, high = 75-и."""
    values = sorted(per_date_min(history).values())
    if len(values) < 10:
        return None
    def pct(q):
        return round(values[min(len(values) - 1, int(q * len(values)))], 2)
    return {"low": pct(0.25), "typical": pct(0.5), "high": pct(0.75), "n": len(values)}


def per_date_min(history: dict[str, list[Interval]]) -> dict[str, float]:
    out = {}
    for fd, intervals in history.items():
        m, _ = min_price(intervals)
        if m is not None:
            out[fd] = m
    return out


def month_stats(out_hist: dict[str, list[Interval]], in_hist: dict[str, list[Interval]]) -> list[dict]:
    """Типична най-ниска цена отиване + връщане по месец (от цялата история).
    За всяка дата се взема най-ниската видяна цена; по месеци – медианата."""
    out_min, in_min = per_date_min(out_hist), per_date_min(in_hist)
    by_month: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"out": [], "in": []})
    for fd, p in out_min.items():
        by_month[fd[:7]]["out"].append(p)
    for fd, p in in_min.items():
        by_month[fd[:7]]["in"].append(p)
    rows = []
    for ym in sorted(by_month):
        o, i = by_month[ym]["out"], by_month[ym]["in"]
        if not o or not i:
            continue
        rows.append({
            "month": ym,
            "out": round(statistics.median(o), 2),
            "in": round(statistics.median(i), 2),
            "total": round(statistics.median(o) + statistics.median(i), 2),
            "min_total": round(min(o) + min(i), 2),
            "days": len(o) + len(i),
        })
    return rows


def weekday_stats(history: dict[str, list[Interval]]) -> list[float | None]:
    """Медиана на най-ниската цена по ден от седмицата на полета (пн … нд)."""
    by_wd: dict[int, list[float]] = defaultdict(list)
    for fd, p in per_date_min(history).items():
        by_wd[date.fromisoformat(fd).weekday()].append(p)
    return [round(statistics.median(by_wd[w]), 2) if by_wd.get(w) else None for w in range(7)]


def season_labels(months: list[dict]) -> dict[str, str]:
    """По месец от годината (01…12): евтино / нормално / скъпо спрямо медианата
    на маршрута. Ако има няколко години, се взема средното за месеца."""
    by_m: dict[str, list[float]] = defaultdict(list)
    for r in months:
        by_m[r["month"][5:7]].append(r["total"])
    if not by_m:
        return {}
    typical = {m: statistics.median(v) for m, v in by_m.items()}
    base = statistics.median(typical.values())
    out = {}
    for m, t in typical.items():
        out[m] = "cheap" if t <= base * 0.85 else "expensive" if t >= base * 1.15 else "normal"
    return out
