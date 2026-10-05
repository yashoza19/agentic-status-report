"""Map Jira keys in evidence to human-readable link phrases."""

from __future__ import annotations

import re
from typing import Any

JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
JIRA_BROWSE_RE = re.compile(
    r"https://(?:issues\.redhat\.com|redhat\.atlassian\.net)/browse/([A-Z][A-Z0-9]+-\d+)"
)
MILESTONE_PREFIX_RE = re.compile(r"^m\d+(?:\.\d+)?:\s*", re.IGNORECASE)
MILESTONE_ONLY_RE = re.compile(r"\bM\d+(?:\.\d+)?\b", re.IGNORECASE)
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def jira_browse_url(key: str, base_url: str = "https://redhat.atlassian.net") -> str:
    return f"{base_url.rstrip('/')}/browse/{key}"


def jira_keys_from_evidence(evidence: list[str]) -> list[str]:
    """Return unique Jira keys referenced in an evidence array."""
    keys: list[str] = []
    seen: set[str] = set()
    for item in evidence:
        key: str | None = None
        if JIRA_KEY_RE.match(item):
            key = item
        else:
            match = JIRA_BROWSE_RE.search(item)
            if match:
                key = match.group(1)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def issue_summary_index(jira_issues: list[dict[str, Any]]) -> dict[str, str]:
    """Build key -> summary from normalized collector issues."""
    summaries: dict[str, str] = {}
    for issue in jira_issues:
        key = issue.get("key")
        summary = issue.get("summary")
        if key and summary:
            summaries[str(key)] = str(summary).strip()
    return summaries


def link_phrase_from_summary(summary: str, *, sentence_case: bool = True) -> str:
    """Turn a Jira summary into mid-sentence link text."""
    phrase = MILESTONE_PREFIX_RE.sub("", summary.strip())
    if not phrase:
        return "related work"
    if sentence_case and phrase[0].isupper() and not (len(phrase) > 1 and phrase[1].isupper()):
        phrase = phrase[0].lower() + phrase[1:]
    return phrase


def build_evidence_labels(
    evidence: list[str],
    issue_summaries: dict[str, str],
    *,
    epic_key: str | None = None,
    epic_name: str | None = None,
) -> dict[str, str]:
    """Map each Jira key in evidence to a link phrase from collector summaries."""
    labels: dict[str, str] = {}
    for key in jira_keys_from_evidence(evidence):
        summary = issue_summaries.get(key)
        if summary:
            labels[key] = link_phrase_from_summary(summary)
        elif epic_key and key == epic_key and epic_name:
            labels[key] = epic_name.strip()
    return labels


def merge_evidence_labels(
    stored: dict[str, str] | None,
    evidence: list[str],
    fetched_summaries: dict[str, str],
    *,
    epic_key: str | None = None,
    epic_name: str | None = None,
) -> dict[str, str]:
    """Combine persisted labels with on-demand Jira summaries for missing keys."""
    labels = dict(stored or {})
    for key in jira_keys_from_evidence(evidence):
        if key in labels:
            continue
        summary = fetched_summaries.get(key)
        if summary:
            labels[key] = link_phrase_from_summary(summary)
        elif epic_key and key == epic_key and epic_name:
            labels[key] = epic_name.strip()
    return labels


def payload_jira_keys(payload: dict[str, Any]) -> set[str]:
    return {
        str(issue["key"])
        for issue in payload.get("jira_issues") or []
        if issue.get("key")
    }


def filter_evidence_to_payload(
    evidence: list[str],
    *,
    allowed_jira_keys: set[str],
    pull_requests: list[dict[str, Any]] | None = None,
    commits: list[dict[str, Any]] | None = None,
    github_activity: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Keep only evidence items grounded in this week's collector payload."""
    pr_urls = {str(pr.get("url")) for pr in (pull_requests or []) if pr.get("url")}
    commit_urls = {str(c.get("url")) for c in (commits or []) if c.get("url")}
    activity_urls = {
        str(value)
        for activity in github_activity or []
        for value in (activity.get("url"), activity.get("subject_url"))
        if value
    }
    kept: list[str] = []
    for item in evidence:
        if JIRA_KEY_RE.match(item):
            if item in allowed_jira_keys:
                kept.append(item)
            continue
        if item in pr_urls or item in commit_urls or item in activity_urls:
            kept.append(item)
            continue
        browse_match = JIRA_BROWSE_RE.search(item)
        if browse_match and browse_match.group(1) in allowed_jira_keys:
            kept.append(item)
            continue
    return kept


def inject_markdown_links(
    text: str,
    evidence_labels: dict[str, str],
    *,
    jira_base_url: str = "https://redhat.atlassian.net",
) -> str:
    """Replace bare Jira keys with [label](url) using evidence_labels."""
    if not text or not evidence_labels:
        return text

    updated = text
    for key in sorted(evidence_labels, key=len, reverse=True):
        url = jira_browse_url(key, jira_base_url)
        if f"]({url})" in updated:
            continue
        label = evidence_labels[key]
        updated = re.sub(
            rf"(?<!\w){re.escape(key)}(?!\w)",
            f"[{label}]({url})",
            updated,
        )
    return updated


def outcome_from_linked_evidence(
    entry_state: str,
    evidence: list[str],
    evidence_labels: dict[str, str],
    *,
    jira_base_url: str = "https://redhat.atlassian.net",
) -> str | None:
    """Build a linked outcome sentence when the model only cited milestone numbers."""
    keys = [key for key in jira_keys_from_evidence(evidence) if key in evidence_labels]
    if not keys:
        return None

    linked = [f"[{evidence_labels[key]}]({jira_browse_url(key, jira_base_url)})" for key in keys]
    if entry_state == "shipped":
        prefix = "Shipped"
    elif entry_state == "blocked":
        prefix = "Blocked on"
    elif entry_state == "slipped":
        prefix = "Slipped on"
    elif entry_state == "quiet":
        prefix = "No activity on"
    else:
        prefix = "Working on"

    if len(linked) == 1:
        return f"{prefix} {linked[0]}."
    if len(linked) == 2:
        return f"{prefix} {linked[0]} and {linked[1]}."
    return f"{prefix} {', '.join(linked[:-1])}, and {linked[-1]}."


def markdown_links_to_slack(text: str) -> str:
    """Convert [label](url) to Slack mrkdwn <url|label>."""
    return MD_LINK_RE.sub(r"<\2|\1>", text)
