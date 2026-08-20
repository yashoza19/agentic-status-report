"""Slack Bolt interactivity handlers."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from status.db import get_session
from status.db.confirm import (
    confirm_draft_entries,
    get_confirmed_entries_for_person,
    get_person_by_slack_id,
    get_unacknowledged_flags,
    latest_unconfirmed_week,
)
from status.db.draft import get_current_drafts
from status.db.repo import get_person
from status.slack.blocks import (
    ACTION_CONFIRM,
    ACTION_EDIT,
    ACTION_REGENERATE,
    build_draft_blocks,
    draft_fallback_text,
)
from status.slack.send import send_draft_review

log = logging.getLogger(__name__)


def parse_action_value(raw: str) -> tuple[str, date]:
    data = json.loads(raw)
    person_id = str(data["person_id"])
    week_ending = date.fromisoformat(str(data["week_ending"]))
    return person_id, week_ending


def _update_message(
    client: Any,
    *,
    channel: str,
    ts: str,
    person_id: str,
    display_name: str,
    week_ending: date,
    confirmed: bool,
) -> None:
    with get_session() as session:
        if confirmed:
            entries = get_confirmed_entries_for_person(session, person_id, week_ending)
        else:
            entries = get_current_drafts(session, person_id, week_ending)
        flags = get_unacknowledged_flags(session, person_id, week_ending)

    blocks = build_draft_blocks(
        person_id=person_id,
        display_name=display_name,
        week_ending=week_ending,
        entries=entries,
        flags=flags,
        confirmed=confirmed,
    )
    client.chat_update(
        channel=channel,
        ts=ts,
        blocks=blocks,
        text=draft_fallback_text(display_name, week_ending, confirmed=confirmed),
    )


def register_handlers(app: Any, *, bot_token: str) -> None:
    """Register Bolt action and slash-command handlers."""

    @app.action(ACTION_CONFIRM)
    def on_confirm(ack: Any, body: dict[str, Any], client: Any) -> None:
        ack()
        action = body["actions"][0]
        person_id, week_ending = parse_action_value(action["value"])
        slack_user_id = body["user"]["id"]

        with get_session() as session:
            person = get_person(session, person_id)
            if person is None:
                log.warning("confirm for unknown person %s", person_id)
                return
            confirm_draft_entries(
                session,
                person_id,
                week_ending,
                confirmed_by=person.person_id,
            )
            display_name = person.display_name

        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        _update_message(
            client,
            channel=channel,
            ts=ts,
            person_id=person_id,
            display_name=display_name,
            week_ending=week_ending,
            confirmed=True,
        )

    @app.action(ACTION_EDIT)
    def on_edit(ack: Any, body: dict[str, Any], client: Any) -> None:
        ack()
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=body["user"]["id"],
            text="Edit flow is coming in the next milestone.",
        )

    @app.action(ACTION_REGENERATE)
    def on_regenerate(ack: Any, body: dict[str, Any], client: Any) -> None:
        ack()
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=body["user"]["id"],
            text="Regenerate flow is coming in the next milestone.",
        )

    @app.command("/status")
    def on_status_command(ack: Any, command: dict[str, Any], client: Any) -> None:
        ack()
        slack_user_id = command["user_id"]
        text = (command.get("text") or "").strip()

        with get_session() as session:
            person = get_person_by_slack_id(session, slack_user_id)
            if person is None:
                client.chat_postEphemeral(
                    channel=command["channel_id"],
                    user=slack_user_id,
                    text="No person record found for your Slack account.",
                )
                return

            if text:
                try:
                    week_ending = date.fromisoformat(text)
                except ValueError:
                    client.chat_postEphemeral(
                        channel=command["channel_id"],
                        user=slack_user_id,
                        text="Usage: `/status` or `/status 2026-08-14`",
                    )
                    return
            else:
                week_ending = latest_unconfirmed_week(session, person.person_id)
                if week_ending is None:
                    client.chat_postEphemeral(
                        channel=command["channel_id"],
                        user=slack_user_id,
                        text="No unconfirmed draft found.",
                    )
                    return

        send_draft_review(person.person_id, week_ending, bot_token=bot_token)
