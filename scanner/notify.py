"""Известия в Slack.

Поддържат се два начина (първият намерен се ползва):
  1. Бот токен  – SLACK_BOT_TOKEN (xoxb-...) + SLACK_CHANNEL_ID
  2. Webhook    – SLACK_WEBHOOK_URL (https://hooks.slack.com/services/...)

Стойностите се четат от променливи на средата. В GitHub Actions те идват от
Settings → Secrets and variables → Actions и никога не се пазят в кода.
"""
from __future__ import annotations

import os

import requests

from .airlines import AIRLINE_NAMES
from .trips import PATTERNS, fmt_day


class SlackNotifier:
    def __init__(self):
        self.token = os.getenv("SLACK_BOT_TOKEN", "").strip()
        self.channel = os.getenv("SLACK_CHANNEL_ID", "").strip()
        self.webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()

    @property
    def mode(self) -> str | None:
        if self.token and self.channel:
            return "bot"
        if self.webhook:
            return "webhook"
        return None

    def send(self, text: str, blocks: list | None = None) -> bool:
        """Връща True при успех. Грешките се логват, но не спират скрипта."""
        payload: dict = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        try:
            if self.mode == "bot":
                resp = requests.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {self.token}",
                             "Content-Type": "application/json; charset=utf-8"},
                    json={"channel": self.channel, "unfurl_links": False, **payload},
                    timeout=15,
                )
                data = resp.json()
                if not data.get("ok") and data.get("error") == "invalid_blocks" and blocks:
                    return self.send(text)
                if not data.get("ok"):
                    print(f"::warning ::Slack отказа съобщението: {data.get('error')}")
                    return False
                return True
            if self.mode == "webhook":
                resp = requests.post(self.webhook, json=payload, timeout=15)
                if resp.status_code == 400 and "invalid_blocks" in resp.text and blocks:
                    return self.send(text)
                if resp.status_code != 200:
                    print(f"::warning ::Slack webhook върна {resp.status_code}: {resp.text[:200]}")
                    return False
                return True
        except requests.RequestException as e:
            print(f"::warning ::Неуспешна връзка със Slack: {e}")
            return False
        print("ℹ️  Slack не е настроен – съобщението само се показва тук:\n" + text)
        return False


def fmt_eur(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} €".replace(",", " ")


def _leg_line(leg) -> str:
    t = f" {leg.dep_time}" if leg.dep_time else ""
    return f"{fmt_day(leg.date)}{t} · {AIRLINE_NAMES.get(leg.airline, leg.airline)} · {fmt_eur(leg.price_eur)}"


def deals_message(deals: list[dict], dashboard_url: str | None):
    """deals: [{route, name, target, trips: [{trip, verdict, ctx, pattern}]}]"""
    n = sum(len(d["trips"]) for d in deals)
    text = f"✈️ {n} {'изгодна комбинация' if n == 1 else 'изгодни комбинации'} под целта"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": text}}]
    for d in deals:
        link = f"{dashboard_url.rstrip('/')}/#{d['route']}" if dashboard_url else None
        title = f"*{d['name']}* ({d['route']})"
        if link:
            title = f"*<{link}|{d['name']}>* ({d['route']})"
        lines = [f"{title} · цел {fmt_eur(d['target'])}"]
        for item in d["trips"]:
            t, v, ctx = item["trip"], item["verdict"], item["ctx"]
            head = f"*{fmt_eur(t.total)}* · {fmt_day(t.out.date)} → {fmt_day(t.back.date)} ({t.nights} нощ.)"
            why = "; ".join(v.reasons[:3])
            emoji = {"buy": "🟢", "good": "🟡", "hold": "⚪", "wait": "🔵"}.get(v.code, "")
            lines.append(f"{head}\n    ↗ {_leg_line(t.out)}\n    ↙ {_leg_line(t.back)}\n"
                         f"    {emoji} *{v.label}*" + (f" – {why}" if why else ""))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})
    hint = "Цените са за 1 възрастен, само билет, в евро по курс на ЕЦБ."
    if dashboard_url:
        hint += f"  <{dashboard_url}|Отвори сайта>"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": hint}]})
    return text, blocks[:50]


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def errors_message(errors: list[dict], total: int):
    text = f"⚠️ {len(errors)} от {total} проверки на полети се провалиха"
    reasons = {e["error"] for e in errors}
    if len(errors) > 1 and len(reasons) == 1:
        reason = reasons.pop()
        names = ", ".join(f"{AIRLINE_NAMES.get(e['airline'], e['airline'])} {e['leg']}" for e in errors[:12])
        body = f"Причина: {_cut(reason, 400)}\n{names}"
    else:
        body = "\n".join(
            f"• {AIRLINE_NAMES.get(e['airline'], e['airline'])} {e['leg']} – {_cut(e['error'], 180)}"
            for e in errors[:12])
    if len(errors) > 12:
        body += f"\n…и още {len(errors) - 12}"
    hint = "Провери flights.yaml и лога в Actions."
    if any("блокира" in e["error"] for e in errors):
        hint = ("Сайтът блокира IP адреса, от който върви проверката. "
                "Виж README → „Проверка от твоя компютър“.")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": _cut(f"*{text}*\n{body}", 2900)}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": hint}]},
    ]
    return text, blocks


def pattern_label(code: str) -> str:
    return PATTERNS.get(code, {}).get("label", code)
