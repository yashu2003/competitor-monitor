"""Slack alert: 'Competitor X published 3 new posts this week' + titles, URLs and keyword data."""
from __future__ import annotations

import httpx

from app import config


def build_message(headline: str, posts: list[dict], pricing_changes: list[str], notes: list[str]) -> str:
    lines = [f"*{headline}*", ""]
    for p in posts[:15]:
        title = (p["title"] or p["url"]).replace("<", "").replace(">", "").replace("|", "-")
        date = p["published_at"].strftime("%d %b %Y") if p.get("published_at") else "date unknown"
        volume = f"{p['search_volume']:,}/mo" if p.get("search_volume") is not None else "n/a"
        lines.append(f"• <{p['url']}|{title}>")
        lines.append(f"     {date}  ·  keyword: `{p.get('keyword') or 'n/a'}`  ·  search volume: {volume}")
    if len(posts) > 15:
        lines.append(f"…and {len(posts) - 15} more")
    if pricing_changes:
        lines += ["", "*Pricing changes*"] + [f"• {c}" for c in pricing_changes]
    if notes:
        lines += ["", "_" + " · ".join(notes) + "_"]
    return "\n".join(lines)


def send_slack(text: str, transport: httpx.BaseTransport | None = None) -> tuple[bool, str]:
    """Returns (sent?, human-readable status). Never raises."""
    url = config.slack_webhook_url()
    if not url:
        return False, "Slack webhook not set, so no alert was sent"
    try:
        with httpx.Client(timeout=15, transport=transport) as client:
            r = client.post(url, json={"text": text})
    except httpx.HTTPError:
        return False, "Couldn't reach Slack, so the alert wasn't sent"
    if r.status_code == 200:
        return True, "Alert sent to Slack"
    return False, f"Slack rejected the alert (HTTP {r.status_code}); check the webhook URL"
