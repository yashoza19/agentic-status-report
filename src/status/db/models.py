from __future__ import annotations

import logging
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class EntryState(str, Enum):
    SHIPPED = "shipped"
    PROGRESSING = "progressing"
    SLIPPED = "slipped"
    BLOCKED = "blocked"
    QUIET = "quiet"


class EntrySource(str, Enum):
    DRAFTED = "drafted"
    DRAFTED_EDITED = "drafted_edited"
    HUMAN_WRITTEN = "human_written"


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ParticipationStatus(str, Enum):
    SENT = "sent"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    ON_LEAVE = "on_leave"
    SEND_FAILED = "send_failed"


class Person(Base):
    __tablename__ = "person"

    person_id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    slack_user_id: Mapped[str | None] = mapped_column(Text, unique=True)
    jira_account_id: Mapped[str | None] = mapped_column(Text, unique=True)
    github_login: Mapped[str | None] = mapped_column(Text)
    manager_id: Mapped[str | None] = mapped_column(Text, ForeignKey("person.person_id"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    entries: Mapped[list[StatusEntry]] = relationship(
        back_populates="person",
        foreign_keys="StatusEntry.person_id",
    )


class Epic(Base):
    __tablename__ = "epic"

    epic_key: Mapped[str] = mapped_column(Text, primary_key=True)
    current_name: Mapped[str] = mapped_column(Text, nullable=False)
    project: Mapped[str] = mapped_column(Text, nullable=False)
    jira_status: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StatusEntry(Base):
    __tablename__ = "status_entry"
    __table_args__ = (
        CheckConstraint("length(btrim(outcome)) > 0", name="outcome_not_blank"),
        CheckConstraint("jsonb_typeof(evidence) = 'array'", name="evidence_is_array"),
        CheckConstraint(
            "extract(isodow from week_ending) = 5",
            name="week_ending_is_friday",
        ),
        Index(
            "status_entry_current_uniq",
            "person_id",
            "week_ending",
            text("coalesce(epic_key, entry_id::text)"),
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("status_entry_week_idx", "week_ending", postgresql_where=text("is_current")),
        Index("status_entry_epic_idx", "epic_key", "week_ending", postgresql_where=text("is_current")),
        Index(
            "status_entry_review_idx",
            "week_ending",
            postgresql_where=text("is_current AND needs_human"),
        ),
        Index("status_entry_evidence_gin", "evidence", postgresql_using="gin"),
    )

    entry_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    week_ending: Mapped[date] = mapped_column(Date, nullable=False)
    person_id: Mapped[str] = mapped_column(Text, ForeignKey("person.person_id"), nullable=False)
    epic_key: Mapped[str | None] = mapped_column(Text, ForeignKey("epic.epic_key"))
    epic_name_snapshot: Mapped[str | None] = mapped_column(Text)
    project: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    blocker: Mapped[str | None] = mapped_column(Text)
    ask: Mapped[str | None] = mapped_column(Text)
    draft_outcome: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str | None] = mapped_column(Text)
    needs_human: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    prompt_version: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    supersedes_entry_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("status_entry.entry_id")
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    drafted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(Text, ForeignKey("person.person_id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    person: Mapped[Person] = relationship(
        back_populates="entries",
        foreign_keys=[person_id],
    )


class Participation(Base):
    __tablename__ = "participation"

    person_id: Mapped[str] = mapped_column(Text, ForeignKey("person.person_id"), primary_key=True)
    week_ending: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    draft_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    regenerated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    regenerate_reason: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class ReportRun(Base):
    __tablename__ = "report_run"

    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    week_ending: Mapped[date] = mapped_column(Date, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    output_uri: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    entries: Mapped[list[ReportEntry]] = relationship(back_populates="report_run")


class ReportEntry(Base):
    __tablename__ = "report_entry"

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("report_run.run_id", ondelete="CASCADE"), primary_key=True
    )
    entry_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("status_entry.entry_id"), primary_key=True
    )
    section: Mapped[str | None] = mapped_column(Text)

    report_run: Mapped[ReportRun] = relationship(back_populates="entries")


class Flag(Base):
    __tablename__ = "flag"
    __table_args__ = (
        Index("flag_week_idx", "week_ending", postgresql_where=text("NOT acknowledged")),
    )

    flag_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    week_ending: Mapped[date] = mapped_column(Date, nullable=False)
    person_id: Mapped[str | None] = mapped_column(Text, ForeignKey("person.person_id"))
    epic_key: Mapped[str | None] = mapped_column(Text, ForeignKey("epic.epic_key"))
    flag_type: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
