"""Draft generation from collector payloads."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from status.config import get_settings
from status.db import get_session
from status.db.draft import persist_draft_output
from status.skills.client import SkillError
from status.skills.evidence import (
    MILESTONE_ONLY_RE,
    build_evidence_labels,
    filter_evidence_to_payload,
    inject_markdown_links,
    issue_summary_index,
    outcome_from_linked_evidence,
    payload_jira_keys,
)
from status.skills.openai_skills import OpenAISkillError
from status.skills.schemas import DraftEntry, DraftOutput
from status.skills.skill_invoke import invoke_skill_json, skill_prompt_version, skill_provider

log = logging.getLogger(__name__)

DRAFTER_INSTRUCTION = (
    "Use the weekly-status-drafter skill on the payload below. "
    "Return only the JSON output defined in the skill as plain text in your reply. "
    "Use markdown links [text](url) in outcome fields for Jira and GitHub evidence."
)


class DraftPersistError(RuntimeError):
    """Raised when draft rows cannot be written to the ledger."""


@dataclass(frozen=True)
class DraftRunResult:
    draft: DraftOutput
    prompt_version: str
    persisted_entry_ids: list[str]
    superseded_count: int


def load_fixture(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"Fixture not found: {resolved}\n"
            "Create one with:\n"
            "  status collect --person <id> --week YYYY-MM-DD "
            "--save-fixture fixtures/payload.json\n"
            "Or omit --fixture and pass --person and --week to collect live."
        )
    return json.loads(resolved.read_text(encoding="utf-8"))


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


def attach_evidence_labels(draft: DraftOutput, payload: dict[str, Any]) -> DraftOutput:
    """Fill per-key link phrases from collector Jira summaries."""
    summaries = issue_summary_index(list(payload.get("jira_issues") or []))
    if not summaries:
        return draft

    enriched: list[DraftEntry] = []
    for entry in draft.entries:
        labels = build_evidence_labels(
            entry.evidence,
            summaries,
            epic_key=entry.epic_key,
            epic_name=entry.epic_name,
        )
        if labels == entry.evidence_labels:
            enriched.append(entry)
        else:
            enriched.append(entry.model_copy(update={"evidence_labels": labels}))
    return draft.model_copy(update={"entries": enriched})


def postprocess_draft(draft: DraftOutput, payload: dict[str, Any]) -> DraftOutput:
    """Filter evidence to this person's payload and enrich outcomes with Jira links."""
    settings = get_settings()
    jira_base_url = settings.jira_base_url or "https://redhat.atlassian.net"
    allowed_keys = payload_jira_keys(payload)
    processed: list[DraftEntry] = []

    for entry in draft.entries:
        evidence = filter_evidence_to_payload(
            entry.evidence,
            allowed_jira_keys=allowed_keys,
            pull_requests=list(payload.get("pull_requests") or []),
            commits=list(payload.get("commits") or []),
        )
        labels = {
            key: label
            for key, label in entry.evidence_labels.items()
            if key in allowed_keys
        }
        outcome = entry.outcome
        if MILESTONE_ONLY_RE.search(outcome) and "[" not in outcome:
            rewritten = outcome_from_linked_evidence(
                entry.state,
                evidence,
                labels,
                jira_base_url=jira_base_url,
            )
            if rewritten:
                outcome = rewritten
        outcome = inject_markdown_links(outcome, labels, jira_base_url=jira_base_url)

        updates: dict[str, Any] = {
            "evidence": evidence,
            "evidence_labels": labels,
            "outcome": outcome,
        }
        if len(evidence) < len(entry.evidence):
            updates["needs_human"] = True
            if not entry.why_flagged:
                updates["why_flagged"] = (
                    "Some cited tickets were removed because they are not assigned to you "
                    "or were not in this week's collector data — OK to keep?"
                )
        processed.append(entry.model_copy(update=updates))

    return draft.model_copy(update={"entries": processed})


def run_drafter(payload: dict[str, Any], *, dry_run: bool = False) -> DraftOutput:
    settings = get_settings()
    if dry_run or not settings.drafter_skill_id:
        return _empty_draft(
            payload,
            flags=["dry-run: no skill invocation"] if dry_run else ["no DRAFTER_SKILL_ID configured"],
        )

    if skill_provider(settings) == "openai":
        if not settings.openai_api_key:
            return _empty_draft(payload, flags=["OPENAI_API_KEY not configured"])
    elif not settings.anthropic_api_key:
        return _empty_draft(payload, flags=["ANTHROPIC_API_KEY not configured"])

    instruction = DRAFTER_INSTRUCTION
    regeneration_notes = str(payload.get("regeneration_notes") or "").strip()
    if regeneration_notes:
        instruction = (
            f"{instruction}\n\nThe user asked to regenerate this draft with this guidance: "
            f"{regeneration_notes}"
        )

    last_error: SkillError | OpenAISkillError | None = None
    for attempt in range(2):
        try:
            result = invoke_skill_json(
                skill_id=settings.drafter_skill_id,
                skill_version=settings.drafter_skill_version,
                payload=payload,
                instruction=instruction,
                schema=DraftOutput,
                settings=settings,
            )
            assert isinstance(result, DraftOutput)
            normalized = _normalize_draft(result, payload)
            labeled = attach_evidence_labels(normalized, payload)
            return postprocess_draft(labeled, payload)
        except (SkillError, OpenAISkillError) as exc:
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
        except IntegrityError as exc:
            raise DraftPersistError(
                "Could not save draft entries — a current row already exists for one "
                "or more epics this week. Re-run `status draft`; if this persists, "
                "check for stale is_current rows in Postgres. "
                f"Details: {exc.orig}"
            ) from exc

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
        prompt_version = skill_prompt_version(
            settings.drafter_skill_id,
            settings.drafter_skill_version,
            settings,
        )
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
        try:
            rows, superseded_count = persist_draft_output(
                session,
                draft,
                prompt_version=prompt_version,
                collection_errors=collection_errors,
            )
        except IntegrityError as exc:
            raise DraftPersistError(
                "Could not save draft entries — a current row already exists for one "
                "or more epics this week. Re-run `status draft` after resolving the conflict."
            ) from exc
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
