"""Slack Block Kit builders for draft review messages."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from status.db.models import Flag, StatusEntry

ACTION_CONFIRM = "status_confirm"
ACTION_EDIT = "status_edit"
ACTION_REGENERATE = "status_regenerate"

STATE_LABELS: dict[str, str] = {
    "shipped": "Shipped",
    "progressing": "In progress",
    "slipped": "Slipped",
    "blocked": "Blocked",
    "quiet": "Quiet",
}


def _action_value(person_id: str, week_ending: date) -> str:
    return json.dumps({"person_id": person_id, "week_ending": week_ending.isoformat()})


def _entry_title(entry: StatusEntry) -> str:
    if entry.epic_name_snapshot:
        return entry.epic_name_snapshot
    if entry.epic_key:
        return entry.epic_key
    return entry.project


def format_entry_text(entry: StatusEntry) -> str:
    label = STATE_LABELS.get(entry.state, entry.state.title())
    lines = [f"*{label}* · {_entry_title(entry)}", entry.outcome]
    if entry.blocker:
        lines.append(f"_Blocker:_ {entry.blocker}")
    if entry.ask:
        lines.append(f"_Ask:_ {entry.ask}")
    if entry.needs_human:
        lines.append("_Needs your review_")
    return "\n".join(lines)


def build_flag_blocks(flags: list[Flag]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for flag in flags[:5]:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":warning: {flag.message}",
                },
            }
        )
    return blocks


def build_draft_blocks(
    *,
    person_id: str,
    display_name: str,
    week_ending: date,
    entries: list[StatusEntry],
    flags: list[Flag],
    confirmed: bool = False,
) -> list[dict[str, Any]]:
    week_label = week_ending.strftime("%b %d, %Y")
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Week ending {week_label}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Draft status for *{display_name}*. Review each entry below.",
            },
        },
    ]
    blocks.extend(build_flag_blocks(flags))

    if not entries:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_No draft entries for this week._",
                },
            }
        )
    else:
        for entry in entries[:20]:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": format_entry_text(entry)},
                }
            )
        if len(entries) > 20:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"_Showing 20 of {len(entries)} entries._",
                        }
                    ],
                }
            )

    if confirmed:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":white_check_mark: *Confirmed* — thanks!",
                },
            }
        )
        return blocks

    value = _action_value(person_id, week_ending)
    blocks.append(
        {
            "type": "actions",
            "block_id": "status_review_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_CONFIRM,
                    "text": {"type": "plain_text", "text": "Looks right"},
                    "style": "primary",
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": ACTION_EDIT,
                    "text": {"type": "plain_text", "text": "Edit"},
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": ACTION_REGENERATE,
                    "text": {"type": "plain_text", "text": "Regenerate"},
                    "value": value,
                },
            ],
        }
    )
    return blocks


def draft_fallback_text(display_name: str, week_ending: date, *, confirmed: bool = False) -> str:
    prefix = "Confirmed" if confirmed else "Draft"
    return f"{prefix} status for {display_name}, week ending {week_ending.isoformat()}"
