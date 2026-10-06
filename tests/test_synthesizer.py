from __future__ import annotations

import re
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from status.db.models import Participation, Person, ReportRun, StatusEntry
from status.db.report import (
    build_citation_index,
    entry_citation_key,
    persist_report_run,
    resolve_cited_entries,
)
from status.skills.schemas import (
    SynthesisEntry,
    SynthesisFlag,
    SynthesisInput,
    SynthesisOutput,
    SynthesisParticipation,
)
from status.skills.synthesizer import (
    build_dry_run_markdown,
    build_synthesis_input,
    default_report_filename,
    limit_visible_github_links,
    management_quality_issues,
    normalize_evidence,
    restore_markdown_structure,
    sanitize_report_markdown,
    strip_gap_commentary,
)


def test_default_report_filename_matches_weekly_status_convention() -> None:
    assert default_report_filename(date(2026, 8, 14)) == "status-2026-08-14.md"


def test_entry_citation_key_uses_unticketed_when_no_epic() -> None:
    entry = MagicMock(person_id="pilot", epic_key=None)
    assert entry_citation_key(entry) == "pilot:unticketed"


def test_build_synthesis_input_uses_display_names_and_omits_review_flags() -> None:
    session = MagicMock()
    week = date(2026, 8, 14)
    person = Person(person_id="pilot", display_name="Pilot User")
    entry = StatusEntry(
        entry_id=uuid4(),
        week_ending=week,
        person_id="pilot",
        project="EET",
        epic_key="EET-5493",
        epic_name_snapshot="Cluster Bot",
        state="progressing",
        outcome="Shipped destroy command.",
        source="drafted",
        evidence=["EET-5500"],
        confirmed_at=datetime.now(UTC),
    )
    quiet_entry = StatusEntry(
        entry_id=uuid4(),
        week_ending=week,
        person_id="pilot",
        project="EET",
        state="quiet",
        outcome="No activity.",
        source="drafted",
        evidence=["EET-5501"],
        confirmed_at=datetime.now(UTC),
    )
    participation = Participation(person_id="pilot", week_ending=week, status="confirmed")

    with (
        patch(
            "status.skills.synthesizer.get_confirmed_entries_for_week",
            return_value=[entry, quiet_entry],
        ),
        patch(
            "status.skills.synthesizer.get_participation_for_week",
            return_value=[participation],
        ),
        patch("status.skills.synthesizer.get_person", return_value=person),
    ):
        payload = build_synthesis_input(session, week)

    assert len(payload.entries) == 1
    assert payload.entries[0].display_name == "Pilot User"
    assert payload.entries[0].report_category == "Certification / CI"
    assert payload.entries[0].report_name == "Cluster Bot"
    assert "https://issues.redhat.com/browse/EET-5500" in payload.entries[0].evidence
    assert payload.participation[0].display_name == "Pilot User"
    assert payload.flags == []


def test_normalize_evidence_adds_jira_urls() -> None:
    evidence = normalize_evidence(["EET-5500", "https://github.com/org/repo/pull/1"])
    assert evidence == [
        "EET-5500",
        "https://issues.redhat.com/browse/EET-5500",
        "https://github.com/org/repo/pull/1",
    ]


def test_build_dry_run_markdown_includes_confirmed_entries() -> None:
    week = date(2026, 8, 14)
    payload = SynthesisInput(
        week_ending=week.isoformat(),
        entries=[
            SynthesisEntry(
                person_id="pilot",
                display_name="Pilot User",
                project="EET",
                epic_key="EET-5493",
                epic_name="Cluster Bot",
                state="progressing",
                outcome="Shipped destroy command.",
                evidence=["EET-5500"],
                report_category="Certification / CI",
                report_name="Cluster Bot",
            )
        ],
        participation=[
            SynthesisParticipation(
                person_id="pilot",
                display_name="Pilot User",
                status="confirmed",
            )
        ],
        flags=[
            SynthesisFlag(message="No calendar signal this week.", person_id="pilot"),
        ],
    )
    markdown = build_dry_run_markdown(payload, week, reason="Dry-run")
    assert "**Cluster Bot** (Pilot User) - Shipped destroy command." in markdown
    assert "## Flags" in markdown
    assert "_Input: 1 entries" in markdown


def test_build_dry_run_markdown_distinguishes_user_and_system_failures() -> None:
    week = date(2026, 8, 14)
    payload = SynthesisInput(
        week_ending=week.isoformat(),
        participation=[
            SynthesisParticipation(
                person_id="expired",
                display_name="Expired User",
                status="expired",
            ),
            SynthesisParticipation(
                person_id="collection",
                display_name="Collection User",
                status="collect_failed",
            ),
            SynthesisParticipation(
                person_id="leave",
                display_name="Leave User",
                status="on_leave",
            ),
        ],
    )

    markdown = build_dry_run_markdown(payload, week, reason="Dry-run")

    assert "No update from Expired User this week." in markdown
    assert "Status evidence collection failed for Collection User." in markdown
    assert "Leave User" not in markdown


def test_sanitize_report_markdown_repairs_bare_paren_jira_urls() -> None:
    payload = SynthesisInput(
        week_ending="2026-08-14",
        entries=[
            SynthesisEntry(
                person_id="pilot",
                display_name="Pilot User",
                project="EET",
                epic_key="EET-5493",
                epic_name="Cluster Bot",
                state="shipped",
                outcome="Closed bot work.",
                evidence=["EET-5493", "EET-5499"],
                evidence_labels={
                    "EET-5493": "Cluster Bot",
                    "EET-5499": "scheduling improvements",
                },
                report_category="Certification / CI",
                report_name="Cluster Bot",
            )
        ],
    )
    raw = (
        "Closed nine of eleven (https://issues.redhat.com/browse/EET-5493) — "
        "[Slack integration](https://issues.redhat.com/browse/EET-5494); "
        "additional (https://issues.redhat.com/browse/EET-5499) also closed."
    )
    cleaned = sanitize_report_markdown(raw, payload)
    assert "[Cluster Bot](https://issues.redhat.com/browse/EET-5493)" in cleaned
    assert "[scheduling improvements](https://issues.redhat.com/browse/EET-5499)" in cleaned
    assert "[Slack integration](https://issues.redhat.com/browse/EET-5494)" in cleaned
    assert re.search(r"(?<!\])\(https://issues\.redhat\.com/browse/EET-5493\)", cleaned) is None


def test_sanitize_report_markdown_unwraps_empty_links_and_drops_notes() -> None:
    raw = (
        "Support for [ITRS] and [SAS] remains waiting.\n\n"
        "## Notes\n\n* Flagging for confirmation."
    )
    cleaned = sanitize_report_markdown(raw)
    assert "ITRS" in cleaned and "[ITRS]" not in cleaned
    assert "## Notes" not in cleaned


def test_restore_markdown_structure_inserts_section_breaks() -> None:
    raw = "# Aug 14, 2026 ## Partner Enablement * **IBM** - Did work."
    cleaned = restore_markdown_structure(raw)
    assert "## Partner Enablement" in cleaned
    assert cleaned.index("## Partner Enablement") > cleaned.index("# Aug 14, 2026")
    assert "\n* **IBM**" in cleaned


def test_sanitize_report_markdown_repairs_github_paren_urls() -> None:
    payload = SynthesisInput(
        week_ending="2026-08-14",
        entries=[
            SynthesisEntry(
                person_id="pilot",
                display_name="Pilot User",
                project="EET",
                epic_key="EET-5493",
                epic_name="Cluster Bot",
                state="shipped",
                outcome="Merged PRs.",
                evidence=["https://github.com/org/repo/pull/26"],
                report_category="Certification / CI",
                report_name="Cluster Bot",
            )
        ],
    )
    raw = "Merged via (https://github.com/org/repo/pull/26)."
    cleaned = sanitize_report_markdown(raw, payload)
    assert "[implementation change](https://github.com/org/repo/pull/26)" in cleaned
    assert re.search(r"(?<!\])\(\s*https://github\.com", cleaned) is None


def test_sanitize_report_markdown_replaces_raw_pr_label_with_outcome_phrase() -> None:
    url = "https://github.com/opdev/agentic-status-report/pull/27"
    payload = SynthesisInput(
        week_ending="2026-09-18",
        entries=[
            SynthesisEntry(
                person_id="yoza",
                display_name="Yash Oza",
                project="opdev/agentic-status-report",
                epic_name="Agentic Weekly Status Pipeline",
                state="progressing",
                outcome=f"Continued [wiring lock-and-report]({url}) through synthesis.",
                evidence=[url],
                report_category="Partner Enablement",
                report_name="Partner Labs",
            )
        ],
    )

    cleaned = sanitize_report_markdown(
        f"* **Partner Labs** - Continued [PR #27]({url}) through synthesis.",
        payload,
    )

    assert f"[wiring lock-and-report]({url})" in cleaned
    assert "PR #27" not in cleaned


def test_limit_visible_github_links_keeps_only_two_per_bullet() -> None:
    raw = (
        "* **Test Suite** - fixed "
        "[lease cleanup](https://github.com/org/repo/pull/1), "
        "[TTL handling](https://github.com/org/repo/pull/2), and "
        "[pull secrets](https://github.com/org/repo/pull/3)."
    )

    cleaned = limit_visible_github_links(raw)

    assert cleaned.count("https://github.com") == 2
    assert "and pull secrets." in cleaned


def test_management_quality_issues_rejects_ordinal_placeholders() -> None:
    markdown = (
        "## Partner Enablement\n\n"
        "* **DH2i** - Advanced deployment UX engagement, with one discussion item "
        "completed and another item in progress."
    )

    assert management_quality_issues(markdown) == [
        "another item",
        "one discussion item",
    ]


def test_sanitize_report_restores_authoritative_name_and_drops_evidence_checklist() -> None:
    payload = SynthesisInput(
        week_ending="2026-09-25",
        entries=[
            SynthesisEntry(
                person_id="yoza",
                display_name="Yash Oza",
                project="EET",
                epic_key="EET-5519",
                epic_name="Agentic Weekly Status Pipeline",
                state="progressing",
                outcome="Improved the weekly status pipeline.",
                evidence=["https://github.com/opdev/agentic-status-report/pull/29"],
                report_category="Partner Enablement",
                report_name="Partner Labs",
            )
        ],
    )
    raw = (
        "# Sep 25, 2026\n\n## Partner Enablement\n\n"
        "* **Agentic Weekly Status Pipeline** - Improved the pipeline; see "
        "implementation change, implementation change, and implementation change; "
        "supporting PRs: #30, #31, and #32."
    )

    cleaned = sanitize_report_markdown(raw, payload)

    assert "* **Partner Labs** - Improved the pipeline." in cleaned
    assert "Agentic Weekly Status Pipeline**" not in cleaned
    assert "implementation change" not in cleaned
    assert "supporting PRs" not in cleaned


def test_sanitize_report_markdown_strips_gap_commentary() -> None:
    raw = (
        "* **OpenShift Cluster Management Bot** - Merged [PR #30](https://github.com/org/repo/pull/30); "
        "this work has no linked Jira tickets despite three PRs merging this week."
    )
    cleaned = sanitize_report_markdown(raw)
    assert "no linked Jira tickets" not in cleaned
    assert "despite three PRs merging this week" not in cleaned
    assert "Merged [implementation change]" in cleaned


def test_strip_gap_commentary_removes_audit_phrases() -> None:
    text = (
        "* **Tailscale** - Continued work; no linked commits or PRs were found this week."
    )
    assert "no linked commits or PRs" not in strip_gap_commentary(text)


def test_resolve_cited_entries_accepts_person_epic_keys() -> None:
    week = date(2026, 8, 14)
    entry = StatusEntry(
        entry_id=uuid4(),
        week_ending=week,
        person_id="pilot",
        project="EET",
        epic_key="EET-5493",
        state="shipped",
        outcome="Done.",
        source="drafted",
        evidence=[],
        confirmed_at=datetime.now(UTC),
    )
    index = build_citation_index([entry])
    resolved = resolve_cited_entries(["pilot:EET-5493"], index, [entry])
    assert len(resolved) == 1
    assert resolved[0].entry_id == entry.entry_id


def test_persist_report_run_supersedes_previous_and_links_entries() -> None:
    session = MagicMock()
    week = date(2026, 8, 14)
    entry = StatusEntry(
        entry_id=uuid4(),
        week_ending=week,
        person_id="pilot",
        project="EET",
        epic_key="EET-5493",
        state="shipped",
        outcome="Done.",
        source="drafted",
        evidence=[],
        confirmed_at=datetime.now(UTC),
    )
    output = SynthesisOutput(
        week_ending=week.isoformat(),
        markdown="# Aug 14, 2026",
        sections_used=["Partner Enablement"],
        entries_cited=["pilot:EET-5493"],
        non_responders=[],
        asks=[],
    )

    added: list[object] = []

    def _add(obj: object) -> None:
        added.append(obj)
        if isinstance(obj, ReportRun):
            obj.run_id = uuid4()

    session.add.side_effect = _add
    session.flush.side_effect = lambda: None

    run = persist_report_run(
        session,
        week,
        output,
        prompt_version="weekly-status-synthesizer@latest",
        model="claude-sonnet-5",
        confirmed_entries=[entry],
        output_uri="status-2026-08-14.md",
        delivered=True,
    )

    assert run.output_uri == "status-2026-08-14.md"
    assert run.delivered_at is not None
    assert run.prompt_version == "weekly-status-synthesizer@latest"
    assert run.model == "claude-sonnet-5"
    session.execute.assert_called_once()
    assert any(isinstance(obj, ReportRun) for obj in added)
    assert len([obj for obj in added if obj.__class__.__name__ == "ReportEntry"]) == 1


def test_synthesize_report_deliver_requires_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from status.config import Settings
    from status.skills import synthesizer as synth_module

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://localhost/weekly_status",
        SYNTHESIZER_SKILL_ID="skill_test",
    )
    monkeypatch.setattr(synth_module, "get_settings", lambda: settings)

    session = MagicMock()
    with patch("status.db.get_session") as session_cm:
        session_cm.return_value.__enter__.return_value = session
        session_cm.return_value.__exit__.return_value = None
        with (
            patch.object(synth_module, "get_confirmed_entries_for_week", return_value=[]),
            patch.object(synth_module, "build_synthesis_input") as build_mock,
            patch.object(synth_module, "run_synthesizer_from_payload") as run_mock,
        ):
            from status.skills.schemas import SynthesisInput, SynthesisOutput

            build_mock.return_value = SynthesisInput(
                week_ending="2026-08-14",
                entries=[],
                participation=[],
                flags=[],
            )
            run_mock.return_value = SynthesisOutput(
                week_ending="2026-08-14",
                markdown="# report",
            )
            with pytest.raises(RuntimeError, match="REPORT_CHANNEL_ID"):
                synth_module.synthesize_report(
                    date(2026, 8, 14),
                    dry_run=False,
                    deliver=True,
                    settings=settings,
                )


@pytest.mark.parametrize(
    ("provider", "skill_id", "skill_version", "expected_model"),
    [
        ("openai", "skill_openai_synth", "12", "gpt-6-astra"),
        ("anthropic", "skill_anthropic_synth", "1759178010641129", "claude-sonnet-5"),
    ],
)
def test_synthesize_report_persists_active_provider_metadata(
    provider: str,
    skill_id: str,
    skill_version: str,
    expected_model: str,
) -> None:
    from status.config import Settings
    from status.skills import synthesizer as synth_module

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://localhost/weekly_status",
        SKILL_PROVIDER=provider,
        OPENAI_SKILLS_MODEL="gpt-6-astra",
        CLAUDE_MODEL="claude-sonnet-5",
        SYNTHESIZER_SKILL_ID=skill_id,
        SYNTHESIZER_SKILL_VERSION=skill_version,
    )
    week = date(2026, 8, 14)
    payload = SynthesisInput(week_ending=week.isoformat())
    output = SynthesisOutput(week_ending=week.isoformat(), markdown="# Report")
    session = MagicMock()

    with (
        patch("status.db.get_session") as session_cm,
        patch.object(synth_module, "build_synthesis_input", return_value=payload),
        patch.object(synth_module, "run_synthesizer_from_payload", return_value=output),
        patch.object(synth_module, "get_confirmed_entries_for_week", return_value=[]),
        patch.object(synth_module, "persist_report_run") as persist_mock,
    ):
        session_cm.return_value.__enter__.return_value = session
        session_cm.return_value.__exit__.return_value = None

        synth_module.synthesize_report(
            week,
            dry_run=False,
            persist=True,
            settings=settings,
        )

    assert persist_mock.call_args.kwargs["model"] == expected_model
    assert persist_mock.call_args.kwargs["prompt_version"] == (
        f"{skill_id}@{skill_version}"
    )
