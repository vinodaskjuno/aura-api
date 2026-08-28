"""Render a Notification into each channel's payload shape."""
from __future__ import annotations

from src.services.notifications.base import SEVERITY_EMOJI, Notification


def slack_blocks(n: Notification) -> list[dict]:
    emoji = SEVERITY_EMOJI.get(n.severity, "⚪")
    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"{emoji} {n.title}"[:150]}},
    ]
    if n.body:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": n.body[:2900]}})
    if n.fields:
        blocks.append({"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*{f.get('label','')}*\n{f.get('value','')}"}
            for f in n.fields[:10]]})
    if n.links:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": l.get("label", "Open")},
             "url": l.get("url", "")} for l in n.links[:5] if l.get("url")]})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"Aura · {n.kind} · severity {n.severity}"}]})
    return blocks


def telegram_text(n: Notification) -> str:
    emoji = SEVERITY_EMOJI.get(n.severity, "⚪")
    lines = [f"{emoji} <b>{_esc(n.title)}</b>"]
    if n.body:
        lines += ["", _esc(n.body[:1500])]
    if n.fields:
        lines.append("")
        lines += [f"• <b>{_esc(f.get('label',''))}:</b> {_esc(str(f.get('value','')))}"
                  for f in n.fields[:10]]
    if n.links:
        lines.append("")
        lines += [f'<a href="{l.get("url","")}">{_esc(l.get("label","Open"))}</a>'
                  for l in n.links[:5] if l.get("url")]
    lines += ["", f"<i>Aura · {_esc(n.kind)}</i>"]
    return "\n".join(lines)


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
