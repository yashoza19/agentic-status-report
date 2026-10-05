"""Collect Jira and GitHub activity for one person and one week."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from status.collectors.github import GitHubCollectorError, collect_github_activity
from status.collectors.jira import (
    JiraCollectorError,
    collect_jira_activity,
    filter_person_jira_issues,
)
from status.collectors.payload import build_payload, week_bounds
from status.collectors.person import resolve_person
from status.db import get_session
from status.db.repo import get_previous_confirmed_entries

log = logging.getLogger(__name__)


def run_collect(
    person_id: str,
    week_ending: date,
    *,
    save_fixture: Path | None = None,
    dry_run: bool = False,
    jira_email: str | None = None,
    github_login: str | None = None,
) -> dict[str, Any]:
    week_start, week_end = week_bounds(week_ending)
    errors: list[str] = []

    if dry_run:
        person = resolve_person(
            person_id,
            None,
            jira_email=jira_email,
            github_login=github_login,
        )
        payload = build_payload(person.person_id, week_ending, [], [], [])
        if save_fixture:
            _write_fixture(save_fixture, payload)
        return payload

    previous_entries: list[dict[str, Any]] = []
    try:
        with get_session() as session:
            person = resolve_person(
                person_id,
                session,
                jira_email=jira_email,
                github_login=github_login,
            )
            previous_entries = get_previous_confirmed_entries(
                session,
                person.person_id,
                week_ending,
            )
    except Exception as exc:
        log.debug("postgres unavailable, skipping previous entries: %s", exc)
        person = resolve_person(
            person_id,
            None,
            jira_email=jira_email,
            github_login=github_login,
        )

    jira_issues: list[dict[str, Any]] = []
    pull_requests: list[dict[str, Any]] = []
    commits: list[dict[str, Any]] = []
    github_collaboration: list[dict[str, Any]] = []

    if person.jira_email:
        try:
            jira_issues = collect_jira_activity(person.jira_email, week_start, week_end)
            jira_issues = filter_person_jira_issues(jira_issues, person.jira_email)
        except JiraCollectorError as exc:
            errors.append(f"jira: {exc}")
            log.error("jira collection failed for %s: %s", person.person_id, exc)
        except Exception as exc:
            errors.append(f"jira: {exc}")
            log.exception("unexpected jira error for %s", person.person_id)
    else:
        errors.append(
            f"jira: no email for person {person.person_id} "
            "(seed person.jira_email or fixtures/eet-persons.json)"
        )

    if person.github_login:
        try:
            github_activity = collect_github_activity(person.github_login, week_start, week_end)
            pull_requests = github_activity["pull_requests"]
            commits = github_activity["commits"]
            github_collaboration = github_activity["github_activity"]
        except GitHubCollectorError as exc:
            errors.append(f"github: {exc}")
            log.error("github collection failed for %s: %s", person.person_id, exc)
        except Exception as exc:
            errors.append(f"github: {exc}")
            log.exception("unexpected github error for %s", person.person_id)
    else:
        errors.append(f"github: no github_login for person {person.person_id}")

    payload = build_payload(
        person.person_id,
        week_ending,
        jira_issues,
        pull_requests,
        previous_entries,
        commits=commits,
        github_activity=github_collaboration,
    )
    if errors:
        payload["collection_errors"] = errors

    if save_fixture:
        _write_fixture(save_fixture, payload)

    return payload


def _write_fixture(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
