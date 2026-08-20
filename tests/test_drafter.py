from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from status.skills.drafter import (
    load_fixture,
    run_drafter,
    week_ending_from_payload,
    _empty_draft,
    _normalize_draft,
)
from status.skills.schemas import DraftEntry, DraftOutput


def test_week_ending_from_payload() -> None:
    payload = {"week_end": "2026-08-14", "person": "pilot"}
    assert week_ending_from_payload(payload) == date(2026, 8, 14)


def test_run_drafter_dry_run() -> None:
    payload = {"person": "pilot", "week_end": "2026-08-14", "jira_issues": [], "pull_requests": []}
    result = run_drafter(payload, dry_run=True)
    assert result.person == "pilot"
    assert result.week_ending == "2026-08-14"
    assert result.entries == []
    assert "dry-run" in result.flags[0]


def test_run_drafter_retries_once_on_skill_error() -> None:
    payload = {"person": "pilot", "week_end": "2026-08-14", "jira_issues": [], "pull_requests": []}
    draft = DraftOutput(
        person="pilot",
        week_ending="2026-08-14",
        entries=[
            DraftEntry(
                project="EET",
                epic_key="EET-5493",
                epic_name="Bot",
                state="progressing",
                outcome="Shipped destroy command.",
                evidence=["EET-5500"],
                confidence="high",
            )
        ],
    )

    with patch("status.skills.drafter.get_settings") as settings_mock:
        settings = settings_mock.return_value
        settings.drafter_skill_id = "skill_test"
        settings.drafter_skill_version = "latest"
        settings.anthropic_api_key = "key"
        settings.claude_model = "claude-sonnet-5"

        with patch("status.skills.drafter.SkillClient") as client_cls:
            client = client_cls.return_value
            client.invoke_json.side_effect = [
                __import__("status.skills.client", fromlist=["SkillError"]).SkillError("bad json"),
                draft,
            ]
            result = run_drafter(payload)

    assert result.entries[0].epic_key == "EET-5493"
    assert client.invoke_json.call_count == 2


def test_run_drafter_returns_flagged_empty_after_two_failures() -> None:
    payload = {"person": "pilot", "week_end": "2026-08-14", "jira_issues": [], "pull_requests": []}
    from status.skills.client import SkillError

    with patch("status.skills.drafter.get_settings") as settings_mock:
        settings = settings_mock.return_value
        settings.drafter_skill_id = "skill_test"
        settings.drafter_skill_version = "latest"
        settings.anthropic_api_key = "key"
        settings.claude_model = "claude-sonnet-5"

        with patch("status.skills.drafter.SkillClient") as client_cls:
            client = client_cls.return_value
            client.invoke_json.side_effect = SkillError("still bad")
            result = run_drafter(payload)

    assert result.entries == []
    assert any("failed after retry" in flag for flag in result.flags)
    assert client.invoke_json.call_count == 2


def test_normalize_draft_uses_payload_week_end() -> None:
    payload = {"person": "pilot", "week_end": "2026-08-14"}
    draft = DraftOutput(person="other", week_ending="2026-08-07", entries=[])
    normalized = _normalize_draft(draft, payload)
    assert normalized.person == "pilot"
    assert normalized.week_ending == "2026-08-14"


def test_empty_draft_shape() -> None:
    payload = {"person": "pilot", "week_end": "2026-08-14"}
    draft = _empty_draft(payload, flags=["test flag"])
    assert draft.week_ending == "2026-08-14"
    assert draft.flags == ["test flag"]


def test_load_fixture(tmp_path) -> None:
    path = tmp_path / "payload.json"
    path.write_text('{"person": "pilot", "week_end": "2026-08-14"}')
    assert load_fixture(path)["person"] == "pilot"
