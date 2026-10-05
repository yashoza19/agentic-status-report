from __future__ import annotations

from datetime import date
from unittest.mock import patch

from status.skills.drafter import (
    _empty_draft,
    _normalize_draft,
    build_repository_epic_hints,
    compact_excessive_pr_outcomes,
    draft_quality_issues,
    load_fixture,
    postprocess_draft,
    run_drafter,
    week_ending_from_payload,
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
        settings.skill_provider = "anthropic"
        settings.anthropic_api_key = "key"
        settings.claude_model = "claude-sonnet-5"

        with patch("status.skills.drafter.invoke_skill_json") as invoke_mock:
            invoke_mock.side_effect = [
                __import__("status.skills.client", fromlist=["SkillError"]).SkillError("bad json"),
                draft,
            ]
            result = run_drafter(payload)

    assert result.entries[0].epic_key == "EET-5493"
    assert invoke_mock.call_count == 2


def test_run_drafter_returns_flagged_empty_after_two_failures() -> None:
    payload = {"person": "pilot", "week_end": "2026-08-14", "jira_issues": [], "pull_requests": []}
    from status.skills.client import SkillError

    with patch("status.skills.drafter.get_settings") as settings_mock:
        settings = settings_mock.return_value
        settings.drafter_skill_id = "skill_test"
        settings.drafter_skill_version = "latest"
        settings.skill_provider = "anthropic"
        settings.anthropic_api_key = "key"
        settings.claude_model = "claude-sonnet-5"

        with patch("status.skills.drafter.invoke_skill_json") as invoke_mock:
            invoke_mock.side_effect = SkillError("still bad")
            result = run_drafter(payload)

    assert result.entries == []
    assert any("failed after retry" in flag for flag in result.flags)
    assert invoke_mock.call_count == 2


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


def test_repository_epic_hints_use_confirmed_repository_evidence() -> None:
    payload = {
        "previous_entries": [
            {
                "project": "EET",
                "epic_key": "EET-5519",
                "epic_name": "Agentic Weekly Status Pipeline",
                "outcome": "Improved weekly status automation.",
                "evidence": [
                    "https://github.com/opdev/agentic-status-report/pull/17"
                ],
            }
        ]
    }

    assert build_repository_epic_hints(payload) == [
        {
            "repo": "opdev/agentic-status-report",
            "epic_key": "EET-5519",
            "epic_name": "Agentic Weekly Status Pipeline",
            "project": "EET",
            "basis": "recent confirmed evidence",
        }
    ]


def test_repository_epic_hints_drop_ambiguous_repository() -> None:
    payload = {
        "previous_entries": [
            {
                "project": "EET",
                "epic_key": epic_key,
                "epic_name": epic_key,
                "outcome": f"[{epic_key} work](https://github.com/example/shared/pull/{number})",
                "evidence": [],
            }
            for epic_key, number in [("EET-1", 1), ("EET-2", 2)]
        ]
    }

    assert build_repository_epic_hints(payload) == []


def test_repository_epic_hints_use_linked_github_collaboration() -> None:
    payload = {
        "jira_issues": [
            {
                "key": "EET-5000",
                "epic_key": "EET-4900",
                "epic_name": "Partner certification",
                "project": "EET",
            }
        ],
        "github_activity": [
            {
                "type": "pull_request_review",
                "repo": "org/operator",
                "url": "https://github.com/org/operator/pull/10#pullrequestreview-1",
                "linked_issue_keys": ["EET-5000"],
            }
        ],
    }
    assert build_repository_epic_hints(payload) == [
        {
            "repo": "org/operator",
            "epic_key": "EET-4900",
            "epic_name": "Partner certification",
            "project": "EET",
            "basis": "current linked Jira evidence",
        }
    ]


def test_postprocess_merges_separate_repo_work_into_hinted_epic() -> None:
    pr_url = "https://github.com/opdev/agentic-status-report/pull/40"
    payload = {
        "person": "pilot",
        "week_end": "2026-08-14",
        "jira_issues": [
            {
                "key": "EET-5529",
                "summary": "OpenShift CronJobs and weekly automation",
                "epic_key": "EET-5519",
                "epic_name": "Agentic Weekly Status Pipeline",
                "project": "EET",
                "is_assignee": False,
                "is_reporter": True,
            }
        ],
        "commits": [],
        "pull_requests": [
            {
                "repo": "opdev/agentic-status-report",
                "url": pr_url,
                "title": "Makefile operations",
                "state": "merged",
                "linked_issue_keys": [],
            }
        ],
        "repository_epic_hints": [
            {
                "repo": "opdev/agentic-status-report",
                "epic_key": "EET-5519",
            }
        ],
    }
    draft = DraftOutput(
        person="pilot",
        week_ending="2026-08-14",
        entries=[
            DraftEntry(
                project="EET",
                epic_key="EET-5519",
                epic_name="Agentic Weekly Status Pipeline",
                state="progressing",
                outcome="The Jira item was listed as Done without transition history.",
                evidence=["EET-5529"],
                confidence="low",
                needs_human=True,
                why_flagged="Was this reporter-only ticket completed?",
            ),
            DraftEntry(
                project="opdev/agentic-status-report",
                epic_key=None,
                epic_name=None,
                state="shipped",
                outcome="Merged Makefile operations for local and cluster workflows.",
                evidence=[pr_url],
                confidence="high",
                needs_human=True,
                why_flagged="Which initiative owns this unticketed work?",
            ),
        ],
    )

    with patch("status.skills.drafter.get_settings") as settings_mock:
        settings_mock.return_value.jira_base_url = "https://redhat.atlassian.net"
        result = postprocess_draft(draft, payload)

    assert len(result.entries) == 1
    assert result.entries[0].epic_key == "EET-5519"
    assert result.entries[0].evidence == [pr_url]
    assert result.entries[0].outcome == (
        "Merged Makefile operations for local and cluster workflows."
    )
    assert result.entries[0].state == "shipped"
    assert result.entries[0].needs_human is False
    assert result.flags == []


def test_postprocess_omits_quiet_entry_without_current_evidence() -> None:
    payload = {
        "person": "pilot",
        "week_end": "2026-08-14",
        "jira_issues": [],
        "pull_requests": [],
        "commits": [],
    }
    draft = DraftOutput(
        person="pilot",
        week_ending="2026-08-14",
        entries=[
            DraftEntry(
                project="EET",
                epic_key="EET-5506",
                epic_name="Partner certification",
                state="quiet",
                outcome="No activity was recorded this week.",
                evidence=["EET-5506"],
                confidence="low",
            )
        ],
    )

    with patch("status.skills.drafter.get_settings") as settings_mock:
        settings_mock.return_value.jira_base_url = "https://redhat.atlassian.net"
        result = postprocess_draft(draft, payload)

    assert result.entries == []


def test_draft_quality_rejects_link_list_and_collector_language() -> None:
    draft = DraftOutput(
        person="pilot",
        week_ending="2026-08-14",
        entries=[
            DraftEntry(
                project="EET",
                epic_key="EET-1",
                epic_name="Pipeline",
                state="shipped",
                outcome=(
                    "Merged [one](https://github.com/example/repo/pull/1), "
                    "[two](https://github.com/example/repo/pull/2), and "
                    "[three](https://github.com/example/repo/pull/3)."
                ),
                evidence=[
                    "https://github.com/example/repo/pull/1",
                    "https://github.com/example/repo/pull/2",
                    "https://github.com/example/repo/pull/3",
                ],
                confidence="high",
            )
        ],
        flags=["Three PRs have no linked Jira ticket."],
    )

    issues = draft_quality_issues(
        draft,
        {
            "jira_issues": [
                {
                    "key": "EET-9",
                    "is_assignee": False,
                    "is_reporter": True,
                }
            ]
        },
    )

    assert any("more than two GitHub links" in issue for issue in issues)
    assert "flags expose internal Jira or collector diagnostics" in issues


def test_draft_quality_rejects_reporter_only_jira_evidence() -> None:
    draft = DraftOutput(
        person="pilot",
        week_ending="2026-08-14",
        entries=[
            DraftEntry(
                project="EET",
                epic_key="EET-1",
                epic_name="Pipeline",
                state="shipped",
                outcome="The scheduling ticket reached Done.",
                evidence=["EET-9"],
                confidence="high",
            )
        ],
    )

    issues = draft_quality_issues(
        draft,
        {"jira_issues": [{"key": "EET-9", "is_assignee": False}]},
    )

    assert any("reporter-only Jira work" in issue for issue in issues)


def test_compact_excessive_pr_outcomes_keeps_all_evidence() -> None:
    urls = [f"https://github.com/example/repo/pull/{number}" for number in range(1, 4)]
    draft = DraftOutput(
        person="pilot",
        week_ending="2026-08-14",
        entries=[
            DraftEntry(
                project="EET",
                epic_key="EET-1",
                epic_name="Pipeline",
                state="shipped",
                outcome=" ".join(f"[PR]({url})" for url in urls),
                evidence=urls,
                confidence="high",
            )
        ],
        flags=["Three PRs have no corresponding Jira ticket."],
    )
    payload = {
        "pull_requests": [
            {"url": url, "title": f"Change {number}", "state": "merged"}
            for number, url in enumerate(urls, start=1)
        ]
    }

    result = compact_excessive_pr_outcomes(draft, payload)

    assert result.entries[0].outcome.count("https://github.com/") == 2
    assert "1 related changes" in result.entries[0].outcome
    assert result.entries[0].evidence == urls
    assert result.flags == []
