from __future__ import annotations

from status.skills.drafter import attach_evidence_labels, postprocess_draft
from status.skills.evidence import (
    build_evidence_labels,
    filter_evidence_to_payload,
    inject_markdown_links,
    issue_summary_index,
    jira_keys_from_evidence,
    link_phrase_from_summary,
    markdown_links_to_slack,
    merge_evidence_labels,
    outcome_from_linked_evidence,
)
from status.skills.schemas import DraftEntry, DraftOutput


def test_jira_keys_from_evidence_deduplicates_keys_and_urls() -> None:
    evidence = [
        "EET-5494",
        "https://issues.redhat.com/browse/EET-5494",
        "https://issues.redhat.com/browse/EET-5495",
        "https://github.com/org/repo/pull/1",
    ]
    assert jira_keys_from_evidence(evidence) == ["EET-5494", "EET-5495"]


def test_build_evidence_labels_uses_jira_summaries() -> None:
    summaries = issue_summary_index(
        [
            {"key": "EET-5494", "summary": "Slack integration for cluster bot"},
            {"key": "EET-5496", "summary": "Destroy command support"},
        ]
    )
    labels = build_evidence_labels(
        ["EET-5494", "EET-5496", "EET-5493"],
        summaries,
        epic_key="EET-5493",
        epic_name="OpenShift Cluster Management Bot",
    )
    assert labels["EET-5494"] == "slack integration for cluster bot"
    assert labels["EET-5496"] == "destroy command support"
    assert labels["EET-5493"] == "OpenShift Cluster Management Bot"


def test_merge_evidence_labels_prefers_stored_and_backfills_fetched() -> None:
    labels = merge_evidence_labels(
        {"EET-5494": "Slack integration"},
        ["EET-5494", "EET-5496"],
        {"EET-5496": "Destroy command support"},
    )
    assert labels["EET-5494"] == "Slack integration"
    assert labels["EET-5496"] == "destroy command support"


def test_link_phrase_from_summary_sentence_cases() -> None:
    assert link_phrase_from_summary("Slack integration") == "slack integration"
    assert link_phrase_from_summary("IBM CP4D") == "IBM CP4D"
    assert (
        link_phrase_from_summary("M5: Synthesizer skill and report delivery")
        == "synthesizer skill and report delivery"
    )


def test_inject_markdown_links_replaces_bare_keys() -> None:
    outcome = inject_markdown_links(
        "Working on EET-5528 this week.",
        {"EET-5528": "synthesizer skill and report delivery"},
    )
    assert (
        outcome
        == "Working on [synthesizer skill and report delivery](https://redhat.atlassian.net/browse/EET-5528) this week."
    )


def test_outcome_from_linked_evidence_rewrites_milestone_speak() -> None:
    outcome = outcome_from_linked_evidence(
        "progressing",
        ["EET-5527", "EET-5528"],
        {
            "EET-5527": "edit and regenerate flows for Slack confirmation",
            "EET-5528": "synthesizer skill and report delivery",
        },
    )
    assert "EET-5527" in outcome
    assert "edit and regenerate flows" in outcome


def test_filter_evidence_to_payload_drops_unowned_jira_keys() -> None:
    evidence = filter_evidence_to_payload(
        ["EET-5527", "EET-5528", "https://github.com/org/repo/pull/1"],
        allowed_jira_keys={"EET-5528"},
        pull_requests=[{"url": "https://github.com/org/repo/pull/1"}],
    )
    assert evidence == ["EET-5528", "https://github.com/org/repo/pull/1"]


def test_filter_evidence_to_payload_drops_unknown_urls() -> None:
    evidence = filter_evidence_to_payload(
        ["https://github.com/org/repo/pull/99", "https://example.com/invented"],
        allowed_jira_keys=set(),
        pull_requests=[{"url": "https://github.com/org/repo/pull/1"}],
    )
    assert evidence == []


def test_filter_evidence_to_payload_keeps_attributable_github_activity() -> None:
    review_url = "https://github.com/org/repo/pull/9#pullrequestreview-10"
    evidence = filter_evidence_to_payload(
        [review_url, "https://github.com/org/repo/pull/99"],
        allowed_jira_keys=set(),
        github_activity=[
            {
                "type": "pull_request_review",
                "url": review_url,
                "subject_url": "https://github.com/org/repo/pull/9",
            }
        ],
    )
    assert evidence == [review_url]


def test_markdown_links_to_slack() -> None:
    text = "See [edit flows](https://redhat.atlassian.net/browse/EET-5527)."
    assert text.replace(
        "[edit flows](https://redhat.atlassian.net/browse/EET-5527)",
        "<https://redhat.atlassian.net/browse/EET-5527|edit flows>",
    ) == markdown_links_to_slack(text)


def test_postprocess_draft_rewrites_milestone_outcome() -> None:
    draft = DraftOutput(
        person="yoza",
        week_ending="2026-08-21",
        entries=[
            DraftEntry(
                project="EET",
                epic_key="EET-5519",
                epic_name="Agentic Weekly Status Pipeline",
                state="progressing",
                outcome="M4 and M5 in progress; edit and regenerate flows plus synthesizer skill active.",
                evidence=["EET-5527", "EET-5528"],
                evidence_labels={
                    "EET-5527": "edit and regenerate flows for Slack confirmation",
                    "EET-5528": "synthesizer skill and report delivery",
                },
                confidence="high",
            )
        ],
    )
    payload = {
        "jira_issues": [
            {
                "key": "EET-5528",
                "summary": "M5: Synthesizer skill and report delivery",
                "is_assignee": True,
            }
        ],
        "pull_requests": [],
        "commits": [],
    }
    processed = postprocess_draft(draft, payload)
    entry = processed.entries[0]
    assert "EET-5527" not in entry.evidence
    assert "EET-5528" in entry.evidence
    assert "[synthesizer skill and report delivery]" in entry.outcome
    assert entry.needs_human is True


def test_attach_evidence_labels_enriches_draft_from_collector_payload() -> None:
    draft = DraftOutput(
        person="pilot",
        week_ending="2026-08-14",
        entries=[
            DraftEntry(
                project="EET",
                epic_key="EET-5493",
                epic_name="OpenShift Cluster Management Bot",
                state="shipped",
                outcome="Closed bot work.",
                evidence=["EET-5494", "EET-5496"],
                confidence="high",
            )
        ],
    )
    payload = {
        "person": "pilot",
        "week_end": "2026-08-14",
        "jira_issues": [
            {"key": "EET-5494", "summary": "Slack integration"},
            {"key": "EET-5496", "summary": "Destroy command support"},
        ],
    }
    enriched = attach_evidence_labels(draft, payload)
    assert enriched.entries[0].evidence_labels["EET-5494"] == "slack integration"
    assert enriched.entries[0].evidence_labels["EET-5496"] == "destroy command support"


def test_postprocess_adds_entry_for_pr_only_work() -> None:
    draft = DraftOutput(
        person="yoza",
        week_ending="2026-09-18",
        entries=[],
        flags=[],
        unticketed_prompt="Anything else from report delivery?",
    )
    payload = {
        "jira_issues": [],
        "pull_requests": [
            {
                "url": "https://github.com/opdev/agentic-status-report/pull/17",
                "title": "feat: synthesizer pipeline and report delivery",
                "repo": "opdev/agentic-status-report",
                "state": "merged",
            },
            {
                "url": "https://github.com/opdev/agentic-status-report/pull/27",
                "title": "fix: wire lock-and-report through synthesize_report",
                "repo": "opdev/agentic-status-report",
                "state": "open",
            },
        ],
        "commits": [],
    }

    processed = postprocess_draft(draft, payload)

    assert len(processed.entries) == 1
    entry = processed.entries[0]
    assert entry.project == "opdev/agentic-status-report"
    assert entry.state == "progressing"
    assert entry.evidence == [
        "https://github.com/opdev/agentic-status-report/pull/17",
        "https://github.com/opdev/agentic-status-report/pull/27",
    ]
    assert "Merged [feat: synthesizer pipeline and report delivery]" in entry.outcome
    assert "Working on [fix: wire lock-and-report through synthesize_report]" in entry.outcome
    assert entry.needs_human is True
    assert any("no current-week Jira evidence" in flag for flag in processed.flags)


def test_postprocess_does_not_duplicate_cited_pr() -> None:
    url = "https://github.com/opdev/agentic-status-report/pull/17"
    draft = DraftOutput(
        person="yoza",
        week_ending="2026-09-18",
        entries=[
            DraftEntry(
                project="opdev/agentic-status-report",
                state="shipped",
                outcome=f"Merged [report delivery]({url}).",
                evidence=[url],
                confidence="high",
                needs_human=True,
            )
        ],
    )
    payload = {
        "jira_issues": [],
        "pull_requests": [
            {
                "url": url,
                "title": "report delivery",
                "repo": "opdev/agentic-status-report",
                "state": "merged",
            }
        ],
        "commits": [],
    }

    processed = postprocess_draft(draft, payload)

    assert len(processed.entries) == 1
