"""Resolve person identifiers for collectors."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from status.db.models import Person


@dataclass(frozen=True)
class PersonContext:
    person_id: str
    display_name: str
    jira_account_id: str | None
    github_login: str | None


def resolve_person(
    person_id: str,
    session: Session | None = None,
    *,
    jira_account_id: str | None = None,
    github_login: str | None = None,
) -> PersonContext:
    if session is not None:
        row = session.get(Person, person_id)
        if row is None:
            stmt = select(Person).where(Person.jira_account_id == person_id)
            row = session.scalars(stmt).first()
        if row is not None:
            return PersonContext(
                person_id=row.person_id,
                display_name=row.display_name,
                jira_account_id=jira_account_id or row.jira_account_id,
                github_login=github_login or row.github_login,
            )

    return PersonContext(
        person_id=person_id,
        display_name=person_id,
        jira_account_id=jira_account_id or person_id,
        github_login=github_login or person_id,
    )
