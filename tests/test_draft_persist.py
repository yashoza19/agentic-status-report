from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from status.db.draft import persist_draft_output, supersede_unconfirmed_drafts
from status.skills.schemas import DraftEntry, DraftOutput


def test_supersede_unconfirmed_drafts_marks_rows_not_current() -> None:
    session = MagicMock()
    row = MagicMock(is_current=True, confirmed_at=None)
    session.scalars.return_value.all.return_value = [row]

    count = supersede_unconfirmed_drafts(session, "yoza", date(2026, 8, 14))

    assert count == 1
    assert row.is_current is False


def test_persist_draft_output_creates_status_entries() -> None:
    session = MagicMock()
    session.get.return_value = None
    session.scalars.return_value.first.return_value = None
    session.scalars.return_value.all.return_value = []

    draft = DraftOutput(
        person="yoza",
        week_ending="2026-08-14",
        entries=[
            DraftEntry(
                project="EET",
                epic_key="EET-5493",
                epic_name="OpenShift Cluster Management Bot",
                state="progressing",
                outcome="Shipped destroy and scheduling features.",
                evidence=["EET-5500", "https://github.com/yashoza19/opdev-cluster-bot/pull/26"],
                confidence="high",
            )
        ],
        flags=["No calendar signal this week."],
        unticketed_prompt="Anything outside Jira?",
    )

    rows, superseded = persist_draft_output(
        session,
        draft,
        prompt_version="skill_test@latest",
        collection_errors=["github: timeout"],
    )

    assert superseded == 0
    assert len(rows) == 1
    assert rows[0].outcome == "Shipped destroy and scheduling features."
    assert rows[0].epic_key == "EET-5493"
