"""Normalize collector output into the drafter skill input contract."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def week_bounds(week_ending: date) -> tuple[date, date]:
    """Return (week_start, week_end) for a Friday week_ending date."""
    if week_ending.weekday() != 4:
        raise ValueError(f"week_ending must be a Friday, got {week_ending}")
    week_start = week_ending - timedelta(days=6)
    return week_start, week_ending


def resolve_week_ending(week: str | None) -> date:
    """
    Resolve week ending date from user input.
    - If week is None or "auto": return most recent Friday (including today if Friday)
    - Otherwise: parse YYYY-MM-DD and validate it's a Friday
    """
    if week is None or week.lower() == "auto":
        today = date.today()
        days_since_friday = (today.weekday() - 4) % 7
        most_recent_friday = today - timedelta(days=days_since_friday)
        return most_recent_friday

    week_ending = date.fromisoformat(week)
    if week_ending.weekday() != 4:
        raise ValueError(f"week_ending must be a Friday, got {week_ending}")
    return week_ending


def build_payload(
    person_id: str,
    week_ending: date,
    jira_issues: list[dict[str, Any]],
    pull_requests: list[dict[str, Any]],
    previous_entries: list[dict[str, Any]] | None = None,
    commits: list[dict[str, Any]] | None = None,
    github_activity: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    week_start, week_end = week_bounds(week_ending)
    return {
        "person": person_id,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "jira_issues": jira_issues,
        "pull_requests": pull_requests,
        "commits": commits or [],
        "github_activity": github_activity or [],
        "previous_entries": previous_entries or [],
    }
