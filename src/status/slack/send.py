"""Send draft review DMs via Slack."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from status.db import get_session
from status.db.confirm import get_unacknowledged_flags, record_draft_sent
from status.db.draft import get_current_drafts
from status.db.models import Person
from status.db.repo import get_person
from status.slack.blocks import build_draft_blocks, draft_fallback_text

log = logging.getLogger(__name__)


class SlackSendError(RuntimeError):
    pass


def _require_slack_client():
    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        raise SlackSendError(
            "slack-bolt is not installed. Run: pip install -e '.[slack]'"
        ) from exc
    return WebClient


def open_dm_channel(client: Any, slack_user_id: str) -> str:
    response = client.conversations_open(users=slack_user_id)
    channel = response.get("channel", {})
    channel_id = channel.get("id")
    if not channel_id:
        raise SlackSendError(f"could not open DM with Slack user {slack_user_id}")
    return str(channel_id)


def send_draft_review(
    person_id: str,
    week_ending: date,
    *,
    bot_token: str,
) -> dict[str, str]:
    """Post a draft review message to the person's Slack DM."""
    WebClient = _require_slack_client()
    client = WebClient(token=bot_token)

    with get_session() as session:
        person = get_person(session, person_id)
        if person is None:
            raise SlackSendError(f"unknown person: {person_id}")
        if not person.slack_user_id:
            raise SlackSendError(f"person {person_id} has no slack_user_id")

        entries = get_current_drafts(session, person.person_id, week_ending)
        flags = get_unacknowledged_flags(session, person.person_id, week_ending)
        blocks = build_draft_blocks(
            person_id=person.person_id,
            display_name=person.display_name,
            week_ending=week_ending,
            entries=entries,
            flags=flags,
        )
        fallback = draft_fallback_text(person.display_name, week_ending)
        channel_id = open_dm_channel(client, person.slack_user_id)
        record_draft_sent(session, person.person_id, week_ending)

    response = client.chat_postMessage(
        channel=channel_id,
        blocks=blocks,
        text=fallback,
    )
    log.info(
        "sent draft review to %s for week %s",
        person_id,
        week_ending.isoformat(),
    )
    return {
        "channel": str(response["channel"]),
        "ts": str(response["ts"]),
        "person_id": person_id,
        "week_ending": week_ending.isoformat(),
    }
