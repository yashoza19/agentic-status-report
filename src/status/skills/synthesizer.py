"""Management report synthesis from confirmed ledger entries. Implemented in M5."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from status.config import get_settings
from status.db import repo
from status.skills.client import SkillClient, SkillRef
from status.skills.schemas import (
    SynthesisEntry,
    SynthesisFlag,
    SynthesisInput,
    SynthesisOutput,
    SynthesisParticipation,
)

SYNTHESIZER_INSTRUCTION = (
    "Use the weekly-status-synthesizer skill on the payload below. "
    "Return only the JSON output defined in the skill."
)


def build_synthesis_input(session: Session, week_ending: date) -> SynthesisInput:
    entries = repo.get_confirmed_entries_for_week(session, week_ending)
    participation = repo.get_participation_for_week(session, week_ending)

    synthesis_entries: list[SynthesisEntry] = []
    for entry in entries:
        synthesis_entries.append(
            SynthesisEntry(
                person_id=entry.person_id,
                display_name=entry.person_id,
                project=entry.project,
                epic_key=entry.epic_key,
                epic_name=entry.epic_name_snapshot,
                state=entry.state,  # type: ignore[arg-type]
                outcome=entry.outcome,
                blocker=entry.blocker,
                ask=entry.ask,
                evidence=entry.evidence,
            )
        )

    synthesis_participation = [
        SynthesisParticipation(
            person_id=p.person_id,
            display_name=p.person_id,
            status=p.status,  # type: ignore[arg-type]
        )
        for p in participation
    ]

    return SynthesisInput(
        week_ending=week_ending.isoformat(),
        entries=synthesis_entries,
        participation=synthesis_participation,
        flags=[],
    )


def run_synthesizer(
    session: Session,
    week_ending: date,
    *,
    dry_run: bool = False,
) -> SynthesisOutput:
    payload = build_synthesis_input(session, week_ending)
    settings = get_settings()

    if dry_run or not settings.synthesizer_skill_id:
        return SynthesisOutput(
            week_ending=week_ending.isoformat(),
            markdown=f"# {week_ending.strftime('%b %d, %Y')}\n\n_dry-run: no skill invocation_",
            sections_used=[],
            entries_cited=[],
            non_responders=[
                p.display_name for p in payload.participation if p.status == "expired"
            ],
            asks=[e.ask for e in payload.entries if e.ask],
        )

    client = SkillClient(api_key=settings.anthropic_api_key, model=settings.claude_model)
    skill = SkillRef(
        skill_id=settings.synthesizer_skill_id,
        version=settings.synthesizer_skill_version,
    )
    result = client.invoke_json(
        skill,
        payload.model_dump(),
        SYNTHESIZER_INSTRUCTION,
        SynthesisOutput,
    )
    assert isinstance(result, SynthesisOutput)
    return result
