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


def build_payload(
    person_id: str,
    week_ending: date,
    jira_issues: list[dict[str, Any]],
    pull_requests: list[dict[str, Any]],
    previous_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    week_start, week_end = week_bounds(week_ending)
    return {
        "person": person_id,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "jira_issues": jira_issues,
        "pull_requests": pull_requests,
        "previous_entries": previous_entries or [],
    }
