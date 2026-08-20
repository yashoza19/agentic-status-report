"""Draft generation from collector payloads."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from status.config import get_settings
from status.db import get_session
from status.db.draft import persist_draft_output
from status.skills.client import SkillClient, SkillError, SkillRef
from status.skills.schemas import DraftOutput

log = logging.getLogger(__name__)

DRAFTER_INSTRUCTION = (
    "Use the weekly-status-drafter skill on the payload below. "
    "Return only the JSON output defined in the skill as plain text in your reply."
)


@dataclass(frozen=True)
class DraftRunResult:
    draft: DraftOutput
    prompt_version: str
    persisted_entry_ids: list[str]
    superseded_count: int


def load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def week_ending_from_payload(payload: dict[str, Any]) -> date:
    raw = payload.get("week_end") or payload.get("week_ending")
    if not raw:
        raise ValueError("collector payload missing week_end")
    return date.fromisoformat(str(raw))


def _empty_draft(payload: dict[str, Any], flags: list[str]) -> DraftOutput:
    week_ending = week_ending_from_payload(payload).isoformat()
    return DraftOutput.model_validate(
        {
            "person": payload.get("person", "unknown"),
            "week_ending": week_ending,
            "entries": [],
            "flags": flags,
            "unticketed_prompt": "",
        }
    )


def _normalize_draft(draft: DraftOutput, payload: dict[str, Any]) -> DraftOutput:
    week_ending = week_ending_from_payload(payload).isoformat()
    if draft.week_ending == week_ending and draft.person == payload.get("person"):
        return draft
    return draft.model_copy(
        update={
            "person": payload.get("person", draft.person),
            "week_ending": week_ending,
        }
    )


def run_drafter(payload: dict[str, Any], *, dry_run: bool = False) -> DraftOutput:
    settings = get_settings()
    if dry_run or not settings.drafter_skill_id:
        return _empty_draft(
            payload,
            flags=["dry-run: no skill invocation"] if dry_run else ["no DRAFTER_SKILL_ID configured"],
        )

    if not settings.anthropic_api_key:
        return _empty_draft(payload, flags=["ANTHROPIC_API_KEY not configured"])

    client = SkillClient(api_key=settings.anthropic_api_key, model=settings.claude_model)
    skill = SkillRef(
        skill_id=settings.drafter_skill_id,
        version=settings.drafter_skill_version,
    )

    last_error: SkillError | None = None
    for attempt in range(2):
        try:
            result = client.invoke_json(skill, payload, DRAFTER_INSTRUCTION, DraftOutput)
            assert isinstance(result, DraftOutput)
            return _normalize_draft(result, payload)
        except SkillError as exc:
            last_error = exc
            log.warning("drafter attempt %s failed: %s", attempt + 1, exc)

    flag = f"drafter failed after retry: {last_error}" if last_error else "drafter failed after retry"
    return _empty_draft(payload, flags=[flag])


def _persist_with_retry(
    draft: DraftOutput,
    *,
    prompt_version: str,
    collection_errors: list[str],
    max_attempts: int = 8,
    retry_delay_seconds: float = 3.0,
) -> tuple[list[str], int]:
    """Write draft rows after skill invocation; reconnect if port-forward dropped."""
    last_error: OperationalError | None = None
    for attempt in range(max_attempts):
        try:
            with get_session() as session:
                rows, superseded_count = persist_draft_output(
                    session,
                    draft,
                    prompt_version=prompt_version,
                    collection_errors=collection_errors,
                )
                entry_ids = [str(row.entry_id) for row in rows]
                return entry_ids, superseded_count
        except OperationalError as exc:
            last_error = exc
            if attempt >= max_attempts - 1:
                break
            log.warning(
                "persist attempt %s/%s failed (%s); retrying in %ss "
                "(keep port-forward running or restart scripts/port-forward-db.sh)",
                attempt + 1,
                max_attempts,
                exc,
                retry_delay_seconds,
            )
            time.sleep(retry_delay_seconds)

    assert last_error is not None
    raise last_error


def draft_and_persist(
    payload: dict[str, Any],
    *,
    dry_run: bool = False,
    persist: bool = True,
    session: Session | None = None,
) -> DraftRunResult:
    """Run the drafter skill, then persist — DB is touched only at the end."""
    settings = get_settings()
    draft = run_drafter(payload, dry_run=dry_run)

    if settings.drafter_skill_id and not dry_run:
        prompt_version = SkillRef(
            skill_id=settings.drafter_skill_id,
            version=settings.drafter_skill_version,
        ).prompt_version
    else:
        prompt_version = "dry-run"

    if dry_run or not persist:
        return DraftRunResult(
            draft=draft,
            prompt_version=prompt_version,
            persisted_entry_ids=[],
            superseded_count=0,
        )

    collection_errors = list(payload.get("collection_errors") or [])
    if session is not None:
        rows, superseded_count = persist_draft_output(
            session,
            draft,
            prompt_version=prompt_version,
            collection_errors=collection_errors,
        )
        persisted_entry_ids = [str(row.entry_id) for row in rows]
    else:
        persisted_entry_ids, superseded_count = _persist_with_retry(
            draft,
            prompt_version=prompt_version,
            collection_errors=collection_errors,
        )
    return DraftRunResult(
        draft=draft,
        prompt_version=prompt_version,
        persisted_entry_ids=persisted_entry_ids,
        superseded_count=superseded_count,
    )
