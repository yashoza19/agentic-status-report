"""Confirm draft entries and track participation."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from status.db.draft import get_current_drafts
from status.db.models import Flag, Participation, Person, StatusEntry


def get_person_by_slack_id(session: Session, slack_user_id: str) -> Person | None:
    stmt = select(Person).where(Person.slack_user_id == slack_user_id)
    return session.scalars(stmt).first()


def get_unacknowledged_flags(
    session: Session,
    person_id: str,
    week_ending: date,
) -> list[Flag]:
    stmt = select(Flag).where(
        Flag.person_id == person_id,
        Flag.week_ending == week_ending,
        Flag.acknowledged.is_(False),
    )
    return list(session.scalars(stmt).all())


def latest_unconfirmed_week(session: Session, person_id: str) -> date | None:
    stmt = (
        select(StatusEntry.week_ending)
        .where(
            StatusEntry.person_id == person_id,
            StatusEntry.is_current.is_(True),
            StatusEntry.confirmed_at.is_(None),
        )
        .order_by(desc(StatusEntry.week_ending))
        .limit(1)
    )
    return session.scalars(stmt).first()


def latest_confirmed_week(session: Session, person_id: str) -> date | None:
    stmt = (
        select(StatusEntry.week_ending)
        .where(
            StatusEntry.person_id == person_id,
            StatusEntry.is_current.is_(True),
            StatusEntry.confirmed_at.is_not(None),
        )
        .order_by(desc(StatusEntry.week_ending))
        .limit(1)
    )
    return session.scalars(stmt).first()


def record_draft_sent(session: Session, person_id: str, week_ending: date) -> Participation:
    now = datetime.now(timezone.utc)
    row = session.get(Participation, (person_id, week_ending))
    if row is None:
        row = Participation(
            person_id=person_id,
            week_ending=week_ending,
            status="sent",
            draft_sent_at=now,
        )
        session.add(row)
    else:
        row.status = "sent"
        row.draft_sent_at = now
    session.flush()
    return row


def confirm_draft_entries(
    session: Session,
    person_id: str,
    week_ending: date,
    *,
    confirmed_by: str,
) -> list[StatusEntry]:
    """Mark all unconfirmed current drafts as confirmed for this person/week."""
    now = datetime.now(timezone.utc)
    entries = get_current_drafts(session, person_id, week_ending)
    if not entries:
        return []

    for entry in entries:
        entry.confirmed_at = now
        entry.confirmed_by = confirmed_by

    row = session.get(Participation, (person_id, week_ending))
    if row is None:
        session.add(
            Participation(
                person_id=person_id,
                week_ending=week_ending,
                status="confirmed",
                confirmed_at=now,
            )
        )
    else:
        row.status = "confirmed"
        row.confirmed_at = now

    session.flush()
    return entries


def get_confirmed_entries_for_person(
    session: Session,
    person_id: str,
    week_ending: date,
) -> list[StatusEntry]:
    stmt = select(StatusEntry).where(
        StatusEntry.person_id == person_id,
        StatusEntry.week_ending == week_ending,
        StatusEntry.is_current.is_(True),
        StatusEntry.confirmed_at.is_not(None),
    )
    return list(session.scalars(stmt).all())
