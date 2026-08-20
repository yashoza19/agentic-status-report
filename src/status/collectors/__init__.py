"""Collect Jira and GitHub activity for one person and one week."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from status.collectors.payload import build_payload


def run_collect(
    person_id: str,
    week_ending: date,
    *,
    save_fixture: Path | None = None,
    dry_run: bool = False,
    jira_account_id: str | None = None,
    github_login: str | None = None,
) -> dict:
    _ = dry_run, jira_account_id, github_login
    payload = build_payload(person_id, week_ending, [], [], [])
    if save_fixture:
        save_fixture.parent.mkdir(parents=True, exist_ok=True)
        save_fixture.write_text(json.dumps(payload, indent=2) + "\n")
    return payload
