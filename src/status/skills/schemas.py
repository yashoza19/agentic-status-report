from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DraftEntry(BaseModel):
    project: str
    epic_key: str | None = None
    epic_name: str | None = None
    state: Literal["shipped", "progressing", "slipped", "blocked", "quiet"]
    outcome: str
    evidence: list[str] = Field(min_length=1)
    blocker: str | None = None
    ask: str | None = None
    confidence: Literal["high", "medium", "low"]
    needs_human: bool = False
    why_flagged: str | None = None


class DraftOutput(BaseModel):
    person: str
    week_ending: str
    entries: list[DraftEntry] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    unticketed_prompt: str = ""


class SynthesisParticipation(BaseModel):
    person_id: str
    display_name: str
    status: Literal["confirmed", "expired", "on_leave", "sent", "send_failed"]


class SynthesisFlag(BaseModel):
    message: str
    person_id: str | None = None
    epic_key: str | None = None


class SynthesisEntry(BaseModel):
    person_id: str
    display_name: str
    project: str
    epic_key: str | None = None
    epic_name: str | None = None
    state: Literal["shipped", "progressing", "slipped", "blocked", "quiet"]
    outcome: str
    blocker: str | None = None
    ask: str | None = None
    evidence: list[str] = Field(default_factory=list)


class SynthesisInput(BaseModel):
    week_ending: str
    entries: list[SynthesisEntry] = Field(default_factory=list)
    participation: list[SynthesisParticipation] = Field(default_factory=list)
    flags: list[SynthesisFlag] = Field(default_factory=list)


class SynthesisOutput(BaseModel):
    week_ending: str
    markdown: str
    sections_used: list[str] = Field(default_factory=list)
    entries_cited: list[str] = Field(default_factory=list)
    non_responders: list[str] = Field(default_factory=list)
    asks: list[str] = Field(default_factory=list)
