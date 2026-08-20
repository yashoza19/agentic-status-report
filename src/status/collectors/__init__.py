"""Collect Jira and GitHub activity for one person and one week."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from status.collectors.github import GitHubCollectorError, collect_github_activity
from status.collectors.jira import JiraCollectorError, collect_jira_activity
from status.collectors.payload import build_payload, week_bounds
from status.collectors.person import PersonContext, resolve_person
from status.config import get_settings
from status.db import get_session
from status.db.repo import get_previous_confirmed_entries

log = logging.getLogger(__name__)


def run_collect(
    person_id: str,
    week_ending: date,
    *,
    save_fixture: Path | None = None,
    dry_run: bool = False,
    jira_account_id: str | None = None,
    github_login: str | None = None,
) -> dict:
    week_start, week_end = week_bounds(week_ending)
    errors: list[str] = []

    if dry_run:
        person = resolve_person(
            person_id,
            None,
            jira_account_id=jira_account_id,
            github_login=github_login,
        )
        payload = build_payload(person.person_id, week_ending, [], [], [])
        if save_fixture:
            _write_fixture(save_fixture, payload)
        return payload

    settings = get_settings()
    if jira_account_id is None:
        jira_account_id = settings.jira_account_id or settings.jira_email
    if github_login is None and settings.github_login:
        github_login = settings.github_login

    previous_entries: list[dict] = []
    try:
        with get_session() as session:
            person = resolve_person(
                person_id,
                session,
                jira_account_id=jira_account_id,
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
            jira_account_id=jira_account_id,
            github_login=github_login,
        )

    jira_issues: list[dict] = []
    pull_requests: list[dict] = []

    if person.jira_account_id:
        try:
            jira_issues = collect_jira_activity(person.jira_account_id, week_start, week_end)
        except JiraCollectorError as exc:
            errors.append(f"jira: {exc}")
            log.error("jira collection failed for %s: %s", person.person_id, exc)
        except Exception as exc:
            errors.append(f"jira: {exc}")
            log.exception("unexpected jira error for %s", person.person_id)
    else:
        errors.append("jira: no account id for person")

    if person.github_login:
        try:
            pull_requests = collect_github_activity(person.github_login, week_start, week_end)
        except GitHubCollectorError as exc:
            errors.append(f"github: {exc}")
            log.error("github collection failed for %s: %s", person.person_id, exc)
        except Exception as exc:
            errors.append(f"github: {exc}")
            log.exception("unexpected github error for %s", person.person_id)
    else:
        errors.append("github: no login for person")

    payload = build_payload(
        person.person_id,
        week_ending,
        jira_issues,
        pull_requests,
        previous_entries,
    )
    if errors:
        payload["collection_errors"] = errors

    if save_fixture:
        _write_fixture(save_fixture, payload)

    return payload


def _write_fixture(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
