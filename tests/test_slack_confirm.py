from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from uuid import uuid4

from status.db.confirm import confirm_draft_entries, record_draft_sent
from status.db.models import Participation, Person, StatusEntry
from status.slack.blocks import (
    ACTION_CONFIRM,
    build_draft_blocks,
    format_entry_text,
)


def test_format_entry_text_includes_state_and_outcome() -> None:
    entry = StatusEntry(
        entry_id=uuid4(),
        week_ending=date(2026, 8, 14),
        person_id="pilot",
        epic_key="EET-5493",
        epic_name_snapshot="OpenShift Cluster Management Bot",
        project="EET",
        state="progressing",
        outcome="Shipped destroy command.",
        source="drafted",
    )
    text = format_entry_text(entry)
    assert "In progress" in text
    assert "Shipped destroy command." in text


def test_build_draft_blocks_includes_confirm_button() -> None:
    entry = StatusEntry(
        entry_id=uuid4(),
        week_ending=date(2026, 8, 14),
        person_id="pilot",
        project="EET",
        state="shipped",
        outcome="Done.",
        source="drafted",
    )
    blocks = build_draft_blocks(
        person_id="pilot",
        display_name="Pilot User",
        week_ending=date(2026, 8, 14),
        entries=[entry],
        flags=[],
    )
    actions = [b for b in blocks if b.get("type") == "actions"]
    assert actions
    button_ids = [el["action_id"] for el in actions[0]["elements"]]
    assert ACTION_CONFIRM in button_ids


def test_build_draft_blocks_confirmed_omits_actions() -> None:
    blocks = build_draft_blocks(
        person_id="pilot",
        display_name="Pilot User",
        week_ending=date(2026, 8, 14),
        entries=[],
        flags=[],
        confirmed=True,
    )
    assert not any(b.get("type") == "actions" for b in blocks)


def test_confirm_draft_entries_sets_confirmed_at() -> None:
    session = MagicMock()
    entry = MagicMock(
        confirmed_at=None,
        confirmed_by=None,
        is_current=True,
    )
    session.scalars.return_value.all.return_value = [entry]
    session.get.return_value = None

    rows = confirm_draft_entries(
        session,
        "pilot",
        date(2026, 8, 14),
        confirmed_by="pilot",
    )

    assert len(rows) == 1
    assert entry.confirmed_at is not None
    assert entry.confirmed_by == "pilot"
    session.add.assert_called_once()


def test_record_draft_sent_creates_participation() -> None:
    session = MagicMock()
    session.get.return_value = None

    row = record_draft_sent(session, "pilot", date(2026, 8, 14))

    assert isinstance(row, Participation)
    assert row.status == "sent"
    session.add.assert_called_once()
