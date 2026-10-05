"""Draft generation from collector payloads."""

from __future__ import annotations

import json
import logging
import re
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
    jira_keys_from_evidence,
    outcome_from_linked_evidence,
    payload_jira_keys,
)
from status.skills.openai_skills import OpenAISkillError
from status.skills.schemas import DraftEntry, DraftOutput
from status.skills.skill_invoke import invoke_skill_json, skill_prompt_version, skill_provider

log = logging.getLogger(__name__)

GITHUB_REPO_URL_RE = re.compile(
    r"https://github\.com/(?P<repo>[^/\s)]+/[^/\s)]+)(?:/(?:pull|commit)/|[\s)]|$)"
)
INTERNAL_DRAFT_LANGUAGE_RE = re.compile(
    r"listed as|supplied data|transition history|recorded only as reporter|"
    r"conflicted with the description|no (?:linked|corresponding) jira|"
    r"assigned to .+ reporter",
    re.IGNORECASE,
)

DRAFTER_INSTRUCTION = (
    "Use the weekly-status-drafter skill on the payload below. "
    "Return only the JSON output defined in the skill as plain text in your reply. "
    "Use markdown links [text](url) in outcome fields for Jira and GitHub evidence. "
    "Critical review constraints: summarize the technical purpose of related PRs instead "
    "of listing them; use at most two GitHub links in any outcome and keep every supporting "
    "URL in evidence; accept an unambiguous repository_epic_hint when the subjects match; "
    "and omit collector diagnostics about missing Jira links, assignees, reporters, payloads, "
    "or transition history. A Jira issue with is_assignee=false may supply its parent epic as "
    "grouping context, but cite it only when activity_role=collaborator proves this person "
    "commented or transitioned it. Treat github_activity as attributable collaboration: say "
    "opened, reviewed, requested changes, approved, or commented as recorded; never claim the "
    "person authored or merged the underlying PR when they only reviewed it."
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
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"Fixture must contain a JSON object: {resolved}")
    return payload


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


def _github_repo_from_text(value: object) -> str | None:
    match = GITHUB_REPO_URL_RE.search(str(value or ""))
    return match.group("repo") if match else None


def build_repository_epic_hints(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Build unambiguous repository-to-epic context from grounded evidence."""
    candidates: dict[str, dict[str, dict[str, str]]] = {}

    def add_hint(
        repo: str | None,
        *,
        epic_key: object,
        epic_name: object,
        project: object,
        basis: str,
    ) -> None:
        key = str(epic_key or "").strip()
        if not repo or not key:
            return
        candidates.setdefault(repo, {})[key] = {
            "repo": repo,
            "epic_key": key,
            "epic_name": str(epic_name or "").strip(),
            "project": str(project or "").strip(),
            "basis": basis,
        }

    issues = {
        str(issue.get("key")): issue
        for issue in payload.get("jira_issues") or []
        if issue.get("key")
    }
    for artifact in [
        *(payload.get("pull_requests") or []),
        *(payload.get("commits") or []),
        *(payload.get("github_activity") or []),
    ]:
        repo = str(artifact.get("repo") or "").strip() or _github_repo_from_text(
            artifact.get("url")
        )
        for issue_key in artifact.get("linked_issue_keys") or []:
            issue = issues.get(str(issue_key))
            if issue:
                add_hint(
                    repo,
                    epic_key=issue.get("epic_key"),
                    epic_name=issue.get("epic_name"),
                    project=issue.get("project"),
                    basis="current linked Jira evidence",
                )

    for previous in payload.get("previous_entries") or []:
        values = [previous.get("outcome"), *(previous.get("evidence") or [])]
        for value in values:
            add_hint(
                _github_repo_from_text(value),
                epic_key=previous.get("epic_key"),
                epic_name=previous.get("epic_name"),
                project=previous.get("project"),
                basis="recent confirmed evidence",
            )

    return [
        next(iter(epics.values()))
        for _repo, epics in sorted(candidates.items())
        if len(epics) == 1
    ]


def _pr_outcome(pull_requests: list[dict[str, Any]]) -> str:
    """Build a concise, fully grounded outcome for repository-only PR work."""
    merged = [pr for pr in pull_requests if pr.get("state") == "merged"]
    ongoing = [pr for pr in pull_requests if pr.get("state") != "merged"]

    def links(rows: list[dict[str, Any]]) -> str:
        visible = rows[:2]
        values = [
            f"[{str(row.get('title') or 'pull request').strip()}]({row['url']})"
            for row in visible
        ]
        if len(rows) > len(visible):
            values.append(f"{len(rows) - len(visible)} related changes")
        if len(values) == 1:
            return values[0]
        return f"{', '.join(values[:-1])}, and {values[-1]}"

    clauses: list[str] = []
    if merged:
        clauses.append(f"Merged {links(merged)}")
    if ongoing:
        clauses.append(f"Working on {links(ongoing)}")
    return ". ".join(clauses) + "."


def _entry_repository(entry: DraftEntry) -> str | None:
    repos = {
        repo
        for value in [entry.outcome, *entry.evidence]
        if (repo := _github_repo_from_text(value))
    }
    if not repos and "/" in entry.project:
        repos.add(entry.project)
    return next(iter(repos)) if len(repos) == 1 else None


def merge_hinted_repository_entries(
    draft: DraftOutput,
    payload: dict[str, Any],
) -> DraftOutput:
    """Fold model-created repository rows into an unambiguous hinted epic."""
    hints = {
        str(hint["repo"]): hint
        for hint in payload.get("repository_epic_hints") or []
        if hint.get("repo") and hint.get("epic_key")
    }
    if not hints:
        return draft

    issues = {
        str(issue.get("key")): issue
        for issue in payload.get("jira_issues") or []
        if issue.get("key")
    }
    pull_requests_by_repo: dict[str, list[dict[str, Any]]] = {}
    for pull_request in payload.get("pull_requests") or []:
        repo = str(pull_request.get("repo") or "").strip()
        if repo:
            pull_requests_by_repo.setdefault(repo, []).append(pull_request)

    entries = list(draft.entries)
    remove_indexes: set[int] = set()
    for source_index, source in enumerate(entries):
        if source.epic_key is not None:
            continue
        source_repo = _entry_repository(source)
        hint = hints.get(source_repo or "")
        if not source_repo or hint is None:
            continue

        pull_requests = pull_requests_by_repo.get(source_repo, [])
        source_outcome = (
            _pr_outcome(pull_requests)
            if len(pull_requests) > 2
            else source.outcome
        )
        target_index = next(
            (
                index
                for index, entry in enumerate(entries)
                if index not in remove_indexes
                and entry.epic_key == str(hint["epic_key"])
            ),
            None,
        )
        mapping_only_question = bool(
            source.why_flagged
            and re.search(r"initiative|unticketed|epic", source.why_flagged, re.IGNORECASE)
        )
        source_updates: dict[str, Any] = {
            "project": str(hint.get("project") or source.project),
            "epic_key": str(hint["epic_key"]),
            "epic_name": str(hint.get("epic_name") or "") or None,
            "outcome": source_outcome,
        }
        if mapping_only_question:
            source_updates.update({"needs_human": False, "why_flagged": None})
        mapped_source = source.model_copy(update=source_updates)

        if target_index is None:
            entries[source_index] = mapped_source
            continue

        target = entries[target_index]
        owned_jira_keys = {
            key
            for key in jira_keys_from_evidence(target.evidence)
            if issues.get(key, {}).get("is_assignee") is True
        }
        if owned_jira_keys:
            outcome = f"{target.outcome.rstrip()} {source_outcome}"
            evidence = list(dict.fromkeys([*target.evidence, *mapped_source.evidence]))
            evidence_labels = {
                **target.evidence_labels,
                **mapped_source.evidence_labels,
            }
            state = (
                "progressing"
                if "progressing" in {target.state, mapped_source.state}
                else mapped_source.state
            )
            needs_human = target.needs_human or mapped_source.needs_human
            why_flagged = target.why_flagged or mapped_source.why_flagged
        else:
            outcome = mapped_source.outcome
            evidence = list(mapped_source.evidence)
            evidence_labels = dict(mapped_source.evidence_labels)
            state = mapped_source.state
            needs_human = mapped_source.needs_human
            why_flagged = mapped_source.why_flagged

        entries[target_index] = target.model_copy(
            update={
                "outcome": outcome,
                "evidence": evidence,
                "evidence_labels": evidence_labels,
                "state": state,
                "confidence": mapped_source.confidence,
                "needs_human": needs_human,
                "why_flagged": why_flagged,
            }
        )
        remove_indexes.add(source_index)

    merged = [entry for index, entry in enumerate(entries) if index not in remove_indexes]
    return draft.model_copy(update={"entries": merged})


def draft_quality_issues(
    draft: DraftOutput,
    payload: dict[str, Any] | None = None,
) -> list[str]:
    """Return management-facing quality failures that warrant one model retry."""
    issues: list[str] = []
    jira_issues = {
        str(issue.get("key")): issue
        for issue in (payload or {}).get("jira_issues") or []
        if issue.get("key")
    }
    for entry in draft.entries:
        if entry.outcome.count("https://github.com/") > 2:
            issues.append(
                f"{entry.epic_key or entry.project} lists more than two GitHub links "
                "instead of summarizing the combined technical result"
            )
        if INTERNAL_DRAFT_LANGUAGE_RE.search(entry.outcome):
            issues.append(
                f"{entry.epic_key or entry.project} uses internal collector language"
            )
        unowned_keys = [
            key
            for key in jira_keys_from_evidence(entry.evidence)
            if jira_issues.get(key, {}).get("is_assignee") is False
        ]
        if unowned_keys:
            issues.append(
                f"{entry.epic_key or entry.project} cites reporter-only Jira work "
                f"({', '.join(unowned_keys)}) instead of using it only as grouping context"
            )
    if any(INTERNAL_DRAFT_LANGUAGE_RE.search(flag) for flag in draft.flags):
        issues.append("flags expose internal Jira or collector diagnostics")
    return list(dict.fromkeys(issues))


def compact_excessive_pr_outcomes(
    draft: DraftOutput,
    payload: dict[str, Any],
) -> DraftOutput:
    """Bound link-heavy outcomes if the model misses the rule after its retry."""
    pull_requests_by_url = {
        str(pr.get("url")): pr
        for pr in payload.get("pull_requests") or []
        if pr.get("url")
    }
    entries: list[DraftEntry] = []
    for entry in draft.entries:
        if entry.outcome.count("https://github.com/") <= 2:
            entries.append(entry)
            continue
        pull_requests = [
            pull_requests_by_url[item]
            for item in entry.evidence
            if item in pull_requests_by_url
        ]
        entries.append(
            entry.model_copy(
                update={"outcome": _pr_outcome(pull_requests)}
                if pull_requests
                else {}
            )
        )
    flags = [
        flag for flag in draft.flags if not INTERNAL_DRAFT_LANGUAGE_RE.search(flag)
    ]
    return draft.model_copy(update={"entries": entries, "flags": flags})


def ensure_pr_only_entries(draft: DraftOutput, payload: dict[str, Any]) -> DraftOutput:
    """Add grounded repository entries for collected PRs the model omitted."""
    cited = {item for entry in draft.entries for item in entry.evidence}
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for pr in payload.get("pull_requests") or []:
        url = str(pr.get("url") or "").strip()
        repo = str(pr.get("repo") or "").strip()
        if not url or not repo or url in cited:
            continue
        by_repo.setdefault(repo, []).append(pr)

    if not by_repo:
        return draft

    entries = list(draft.entries)
    flags = list(draft.flags)
    hints = {
        str(hint["repo"]): str(hint["epic_key"])
        for hint in payload.get("repository_epic_hints") or []
        if hint.get("repo") and hint.get("epic_key")
    }
    for repo, pull_requests in by_repo.items():
        hinted_epic = hints.get(repo)
        matching_index = next(
            (
                index
                for index, entry in enumerate(entries)
                if hinted_epic and entry.epic_key == hinted_epic
            ),
            None,
        )
        if matching_index is not None:
            entry = entries[matching_index]
            evidence = list(
                dict.fromkeys(
                    [*entry.evidence, *(str(pr["url"]) for pr in pull_requests)]
                )
            )
            entries[matching_index] = entry.model_copy(update={"evidence": evidence})
            continue

        entries.append(
            DraftEntry(
                project=repo,
                epic_key=None,
                epic_name=None,
                state=(
                    "shipped"
                    if all(pr.get("state") == "merged" for pr in pull_requests)
                    else "progressing"
                ),
                outcome=_pr_outcome(pull_requests),
                evidence=[str(pr["url"]) for pr in pull_requests],
                confidence="high",
                needs_human=True,
                why_flagged="Which initiative should this unticketed pull-request work roll up to?",
            )
        )
        flags.append(f"{repo} has pull-request activity with no current-week Jira evidence.")
    return draft.model_copy(update={"entries": entries, "flags": flags})


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
            github_activity=list(payload.get("github_activity") or []),
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
                    "Some citations were removed because they are absent from this week's "
                    "collector data; previous entries are context only. Is any removed work "
                    "actually current?"
                )
        processed_entry = entry.model_copy(update=updates)
        if processed_entry.state == "quiet" and not processed_entry.evidence:
            continue
        processed.append(processed_entry)

    processed_draft = merge_hinted_repository_entries(
        draft.model_copy(update={"entries": processed}),
        payload,
    )
    return ensure_pr_only_entries(processed_draft, payload)


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

    skill_payload = dict(payload)
    repository_epic_hints = build_repository_epic_hints(payload)
    if repository_epic_hints:
        skill_payload["repository_epic_hints"] = repository_epic_hints

    instruction = DRAFTER_INSTRUCTION
    regeneration_notes = str(payload.get("regeneration_notes") or "").strip()
    if regeneration_notes:
        instruction = (
            f"{instruction}\n\nThe user asked to regenerate this draft with this guidance: "
            f"{regeneration_notes}"
        )

    last_error: SkillError | OpenAISkillError | None = None
    last_quality_issues: list[str] = []
    for attempt in range(2):
        try:
            result = invoke_skill_json(
                skill_id=settings.drafter_skill_id,
                skill_version=settings.drafter_skill_version,
                payload=skill_payload,
                instruction=instruction,
                schema=DraftOutput,
                settings=settings,
            )
            assert isinstance(result, DraftOutput)
            normalized = _normalize_draft(result, payload)
            labeled = attach_evidence_labels(normalized, payload)
            processed = postprocess_draft(labeled, skill_payload)
            quality_issues = draft_quality_issues(processed, skill_payload)
            if quality_issues and attempt == 0:
                last_quality_issues = quality_issues
                instruction = (
                    f"{DRAFTER_INSTRUCTION}\n\nYour previous draft failed review: "
                    f"{'; '.join(quality_issues)}. Rewrite it rather than defending it."
                )
                log.warning("drafter quality retry: %s", "; ".join(quality_issues))
                continue
            if quality_issues:
                processed = compact_excessive_pr_outcomes(processed, skill_payload)
                unresolved = draft_quality_issues(processed, skill_payload)
                if unresolved:
                    log.error(
                        "drafter failed quality review after retry: %s",
                        "; ".join(unresolved),
                    )
                    return _empty_draft(
                        payload,
                        flags=[
                            (
                                "drafter failed quality review after retry: "
                                f"{'; '.join(unresolved)}"
                            )
                        ],
                    )
            return processed
        except (SkillError, OpenAISkillError) as exc:
            last_error = exc
            log.warning("drafter attempt %s failed: %s", attempt + 1, exc)

    if last_error:
        flag = f"drafter failed after retry: {last_error}"
    elif last_quality_issues:
        flag = f"drafter failed quality review after retry: {'; '.join(last_quality_issues)}"
    else:
        flag = "drafter failed after retry"
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
