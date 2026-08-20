"""Persist drafter output to the ledger."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from status.db.models import EntrySource, Epic, Flag, Person, StatusEntry
from status.skills.schemas import DraftEntry, DraftOutput


def ensure_person(session: Session, person_id: str, *, display_name: str | None = None) -> Person:
    person = session.get(Person, person_id)
    if person is not None:
        return person

    person = Person(person_id=person_id, display_name=display_name or person_id)
    session.add(person)
    session.flush()
    return person


def upsert_epic(session: Session, epic_key: str, epic_name: str, project: str) -> None:
    epic = session.get(Epic, epic_key)
    now = datetime.now(timezone.utc)
    if epic is None:
        session.add(
            Epic(
                epic_key=epic_key,
                current_name=epic_name,
                project=project,
                last_seen_at=now,
            )
        )
        return

    epic.current_name = epic_name
    epic.project = project
    epic.last_seen_at = now


def _parse_week_ending(value: str) -> date:
    return date.fromisoformat(value)


def supersede_unconfirmed_drafts(session: Session, person_id: str, week_ending: date) -> int:
    """Mark unconfirmed current drafts as non-current. Returns rows superseded."""
    stmt = select(StatusEntry).where(
        StatusEntry.person_id == person_id,
        StatusEntry.week_ending == week_ending,
        StatusEntry.is_current.is_(True),
        StatusEntry.confirmed_at.is_(None),
    )
    rows = list(session.scalars(stmt).all())
    for row in rows:
        row.is_current = False
    return len(rows)


def acknowledge_draft_flags(session: Session, person_id: str, week_ending: date) -> int:
    """Acknowledge prior draft-run flags for this person/week before re-drafting."""
    result = session.execute(
        update(Flag)
        .where(
            Flag.person_id == person_id,
            Flag.week_ending == week_ending,
            Flag.acknowledged.is_(False),
        )
        .values(acknowledged=True)
    )
    return int(result.rowcount or 0)


def _entry_extra(entry: DraftEntry) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if entry.why_flagged:
        extra["why_flagged"] = entry.why_flagged
    return extra


def _status_entry_from_draft(
    entry: DraftEntry,
    *,
    person_id: str,
    week_ending: date,
    prompt_version: str,
    drafted_at: datetime,
    supersedes: StatusEntry | None = None,
) -> StatusEntry:
    revision = 1 if supersedes is None else supersedes.revision + 1
    return StatusEntry(
        week_ending=week_ending,
        person_id=person_id,
        epic_key=entry.epic_key,
        epic_name_snapshot=entry.epic_name,
        project=entry.project,
        state=entry.state,
        outcome=entry.outcome,
        blocker=entry.blocker,
        ask=entry.ask,
        draft_outcome=entry.outcome,
        source=EntrySource.DRAFTED.value,
        confidence=entry.confidence,
        needs_human=entry.needs_human,
        prompt_version=prompt_version,
        evidence=list(entry.evidence),
        extra=_entry_extra(entry),
        revision=revision,
        supersedes_entry_id=supersedes.entry_id if supersedes else None,
        is_current=True,
        drafted_at=drafted_at,
        confirmed_at=None,
    )


def _find_superseded_row(
    session: Session,
    person_id: str,
    week_ending: date,
    epic_key: str | None,
) -> StatusEntry | None:
    """Find the most recent superseded unconfirmed row for the same epic grain."""
    stmt = (
        select(StatusEntry)
        .where(
            StatusEntry.person_id == person_id,
            StatusEntry.week_ending == week_ending,
            StatusEntry.is_current.is_(False),
            StatusEntry.confirmed_at.is_(None),
            StatusEntry.epic_key == epic_key if epic_key else StatusEntry.epic_key.is_(None),
        )
        .order_by(StatusEntry.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def persist_draft_output(
    session: Session,
    draft: DraftOutput,
    *,
    prompt_version: str,
    collection_errors: list[str] | None = None,
) -> tuple[list[StatusEntry], int]:
    """Replace unconfirmed drafts for (person, week) with a new draft revision."""
    week_ending = _parse_week_ending(draft.week_ending)
    ensure_person(session, draft.person)
    superseded_count = supersede_unconfirmed_drafts(session, draft.person, week_ending)
    acknowledge_draft_flags(session, draft.person, week_ending)

    drafted_at = datetime.now(timezone.utc)
    saved: list[StatusEntry] = []

    for entry in draft.entries:
        if entry.epic_key and entry.epic_name:
            upsert_epic(session, entry.epic_key, entry.epic_name, entry.project)

        supersedes = _find_superseded_row(session, draft.person, week_ending, entry.epic_key)
        row = _status_entry_from_draft(
            entry,
            person_id=draft.person,
            week_ending=week_ending,
            prompt_version=prompt_version,
            drafted_at=drafted_at,
            supersedes=supersedes,
        )
        session.add(row)
        saved.append(row)

    _persist_flags(
        session,
        draft,
        week_ending,
        collection_errors=collection_errors or [],
    )
    session.flush()
    return saved, superseded_count


def _persist_flags(
    session: Session,
    draft: DraftOutput,
    week_ending: date,
    *,
    collection_errors: list[str],
) -> None:
    for message in draft.flags:
        session.add(
            Flag(
                week_ending=week_ending,
                person_id=draft.person,
                flag_type="draft",
                message=message,
            )
        )

    if draft.unticketed_prompt.strip():
        session.add(
            Flag(
                week_ending=week_ending,
                person_id=draft.person,
                flag_type="unticketed",
                message=draft.unticketed_prompt.strip(),
            )
        )

    for message in collection_errors:
        session.add(
            Flag(
                week_ending=week_ending,
                person_id=draft.person,
                flag_type="collection",
                message=message,
            )
        )

    for entry in draft.entries:
        if entry.why_flagged:
            session.add(
                Flag(
                    week_ending=week_ending,
                    person_id=draft.person,
                    epic_key=entry.epic_key,
                    flag_type="entry",
                    message=entry.why_flagged,
                )
            )


def get_current_drafts(session: Session, person_id: str, week_ending: date) -> list[StatusEntry]:
    stmt = select(StatusEntry).where(
        StatusEntry.person_id == person_id,
        StatusEntry.week_ending == week_ending,
        StatusEntry.is_current.is_(True),
        StatusEntry.confirmed_at.is_(None),
    )
    return list(session.scalars(stmt).all())
