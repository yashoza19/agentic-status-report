"""Management report synthesis from confirmed ledger entries."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from status.config import Settings, get_settings
from status.db.models import StatusEntry
from status.db.repo import (
    get_confirmed_entries_for_week,
    get_participation_for_week,
    get_person,
)
from status.db.report import persist_report_run
from status.skills.evidence import (
    JIRA_BROWSE_RE,
    JIRA_KEY_RE,
    jira_keys_from_evidence,
    merge_evidence_labels,
)
from status.skills.report_style import classify_report_entry
from status.skills.schemas import (
    SynthesisEntry,
    SynthesisInput,
    SynthesisOutput,
    SynthesisParticipation,
)
from status.skills.skill_invoke import invoke_skill_json, skill_provider

log = logging.getLogger(__name__)

SYNTHESIZER_INSTRUCTION = (
    "Use the weekly-status-synthesizer skill on the payload below. "
    "Return only the JSON output defined in the skill as plain text in your reply. "
    "Write the JSON in a text block — do not use the code execution tool to format it."
)

SYNTHESIZER_PROMPT_VERSION = "weekly-status-synthesizer@latest"
SYNTHESIZER_MAX_TOKENS = 16_384

# Bare `(https://issues.redhat.com/browse/KEY)` — not already part of `[text](url)`.
PAREN_JIRA_URL_RE = re.compile(
    r"(?<!\])\(\s*https://(?:issues\.redhat\.com|redhat\.atlassian\.net)/browse/"
    r"([A-Z][A-Z0-9]+-\d+)\s*\)"
)
PAREN_GITHUB_URL_RE = re.compile(r"(?<!\])\(\s*(https://github\.com/[^\s)]+)\s*\)")
EMPTY_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\](?!\()")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
RAW_PR_LINK_RE = re.compile(
    r"\[(?:PR|pull request)\s*#?\d+\]\((https://github\.com/[^)]+/pull/\d+)\)",
    re.IGNORECASE,
)
NOTES_SECTION_RE = re.compile(r"\n## Notes\s*\n.*\Z", re.DOTALL)
GENERIC_EVIDENCE_SUFFIX_RE = re.compile(
    r";\s*see\s+(?:(?:implementation\s+)?change)"
    r"(?:(?:,\s*(?:and\s+)?|\s+and\s+)(?:(?:implementation\s+)?change))*\.??",
    re.IGNORECASE,
)
SUPPORTING_PRS_SUFFIX_RE = re.compile(
    r";\s*supporting\s+PRs?\s*:[^.\n]*\.??",
    re.IGNORECASE,
)
ADDITIONAL_EVIDENCE_SUFFIX_RE = re.compile(
    r"(?:[.;]\s*)?additional\s+evidence\s*:[^\n]*",
    re.IGNORECASE,
)
BACKLOG_ONLY_CLAUSE_RE = re.compile(
    r";\s*(?:the\s+)?\[[^\]]+\]\([^)]+\)\s+"
    r"(?:remained|was)\s+in\s+(?:the\s+)?backlog\.?",
    re.IGNORECASE,
)
BACKLOG_ONLY_OUTCOME_RE = re.compile(
    r"^(?:the\s+)?\[[^\]]+\]\([^)]+\)\s+"
    r"(?:remained|was)\s+in\s+(?:the\s+)?backlog\.?$",
    re.IGNORECASE,
)
RAW_COMMIT_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
REPORT_BULLET_RE = re.compile(r"^(\s*\*\s+\*\*)([^*]+)(\*\*\s+-\s+)(.*)$")
GAP_COMMENTARY_RES = [
    re.compile(r";\s*this work has no linked Jira tickets despite[^.;]*\.?", re.IGNORECASE),
    re.compile(r";\s*no linked commits or PRs were found this week\.?", re.IGNORECASE),
    re.compile(r";\s*[^.;]*backlog status in Jira despite[^.;]*\.?", re.IGNORECASE),
    re.compile(
        r";\s*[^.;]*despite (?:\d+|three|their|\w+) PRs merging this week\.?",
        re.IGNORECASE,
    ),
]
VAGUE_MANAGEMENT_PATTERNS = [
    re.compile(r"\bone discussion item\b", re.IGNORECASE),
    re.compile(r"\banother (?:discussion )?item\b", re.IGNORECASE),
    re.compile(r"\bthe (?:first|second) (?:discussion )?item\b", re.IGNORECASE),
]


def normalize_evidence(evidence: list[str]) -> list[str]:
    """Ensure Jira keys in evidence include browse URLs for the skill."""
    normalized: list[str] = []
    for item in evidence:
        if JIRA_KEY_RE.match(item):
            normalized.append(item)
            normalized.append(f"https://issues.redhat.com/browse/{item}")
        else:
            normalized.append(item)
    return normalized


def strip_inactive_backlog_clauses(outcome: str) -> str:
    """Remove a backlog-only tail that does not describe completed weekly work."""
    cleaned = BACKLOG_ONLY_CLAUSE_RE.sub("", outcome)
    cleaned = BACKLOG_ONLY_OUTCOME_RE.sub("", cleaned.strip())
    cleaned = cleaned.strip().rstrip(";")
    if outcome.rstrip().endswith((".", "!", "?")) and cleaned and not cleaned.endswith(
        (".", "!", "?")
    ):
        cleaned += "."
    return cleaned


def select_visible_evidence(
    outcome: str,
    evidence: list[str],
    *,
    maximum: int = 2,
) -> list[str]:
    """Select display-worthy evidence while leaving the full audit set in Postgres."""
    normalized: list[str] = []
    supported_jira_keys = set(jira_keys_from_evidence(evidence))
    for item in evidence:
        if JIRA_KEY_RE.match(item):
            normalized.append(f"https://issues.redhat.com/browse/{item}")
        elif item not in normalized:
            normalized.append(item)

    outcome_urls: list[str] = []
    for _label, url in MARKDOWN_LINK_RE.findall(outcome):
        jira_match = JIRA_BROWSE_RE.search(url)
        supported = url in normalized or bool(
            jira_match and jira_match.group(1) in supported_jira_keys
        )
        if supported and url not in outcome_urls:
            outcome_urls.append(url)
    if outcome_urls:
        return outcome_urls[:maximum]

    non_commit = [item for item in normalized if "/commit/" not in item]
    commit = [item for item in normalized if "/commit/" in item]
    return [*non_commit, *commit][:maximum]


def build_jira_link_phrases(payload: SynthesisInput) -> dict[str, str]:
    """Map Jira keys to human-readable link text drawn from ledger context."""
    phrases: dict[str, str] = {}

    def add_key(key: str, phrase: str) -> None:
        if key not in phrases and phrase.strip():
            phrases[key] = phrase.strip()

    for entry in payload.entries:
        epic_phrase = entry.epic_name or entry.outcome[:60].rstrip(" .")
        label_map = entry.evidence_labels or {}
        if entry.epic_key:
            add_key(entry.epic_key, label_map.get(entry.epic_key) or epic_phrase)
        for item in entry.evidence:
            key_match = JIRA_KEY_RE.match(item)
            if key_match:
                key = key_match.group(0)
                add_key(key, label_map.get(key) or epic_phrase)
                continue
            browse_match = JIRA_BROWSE_RE.search(item)
            if browse_match:
                key = browse_match.group(1)
                add_key(key, label_map.get(key) or epic_phrase)
    return phrases


def build_github_link_phrases(payload: SynthesisInput) -> dict[str, str]:
    phrases: dict[str, str] = {}
    for entry in payload.entries:
        for label, url in MARKDOWN_LINK_RE.findall(entry.outcome):
            if "github.com" in url.lower() and not re.fullmatch(
                r"(?:PR|pull request)\s*#?\d+", label, re.IGNORECASE
            ):
                phrases[url] = label.strip()
        for item in entry.evidence:
            if "github.com" not in item.lower():
                continue
            if item in phrases:
                continue
            if "/pull/" in item:
                phrases[item] = "implementation change"
            else:
                phrases[item] = "change"
    return phrases


def limit_visible_github_links(text: str, *, maximum: int = 2) -> str:
    """Keep the audit trail intact while limiting links shown in each report bullet."""
    lines: list[str] = []
    for line in text.splitlines():
        if not line.lstrip().startswith("*"):
            lines.append(line)
            continue
        seen = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal seen
            label, url = match.groups()
            if "github.com" not in url.lower():
                return match.group(0)
            seen += 1
            return match.group(0) if seen <= maximum else label

        lines.append(MARKDOWN_LINK_RE.sub(replace, line))
    return "\n".join(lines)


def synthesis_input_quality_issues(payload: SynthesisInput) -> list[str]:
    """Reject generic rollups before spending a hosted synthesizer call."""
    issues: list[str] = []
    known_acronym_names = {"DH2I", "FIS", "HYCU", "IBM"}
    for entry in payload.entries:
        report_name = entry.report_name.strip()
        project = entry.project.strip()
        if (
            report_name == project
            and re.fullmatch(r"[A-Z][A-Z0-9]{1,10}", report_name)
            and report_name.upper() not in known_acronym_names
        ):
            issues.append(
                f"{entry.person_id}:{entry.epic_key or 'unticketed'} has generic "
                f"report label {report_name!r}; add an initiative or partner mapping"
            )
        if len(entry.evidence) > 2:
            issues.append(
                f"{entry.person_id}:{entry.epic_key or 'unticketed'} exposes more than "
                "two evidence links"
            )
    return list(dict.fromkeys(issues))


def management_quality_issues(
    markdown: str,
    payload: SynthesisInput | None = None,
) -> list[str]:
    """Return management-facing placeholder phrases that require richer source data."""
    issues: list[str] = []
    if re.search(r"\badditional\s+evidence\b", markdown, re.IGNORECASE):
        issues.append("additional evidence checklist")
    if re.search(r"\bimplementation\s+change\b", markdown, re.IGNORECASE):
        issues.append("generic implementation change label")
    if RAW_COMMIT_HASH_RE.search(markdown):
        issues.append("raw commit hash")
    if re.search(r"\b(?:remained|was)\s+in\s+(?:the\s+)?backlog\b", markdown, re.IGNORECASE):
        issues.append("backlog-only status")

    allowed_by_category: dict[str, set[str]] = defaultdict(set)
    if payload:
        for entry in payload.entries:
            allowed_by_category[entry.report_category].add(entry.report_name)

    category: str | None = None
    for line in markdown.splitlines():
        if line.startswith("## "):
            category = line[3:].strip()
            continue
        if not line.lstrip().startswith("*"):
            continue
        for pattern in VAGUE_MANAGEMENT_PATTERNS:
            match = pattern.search(line)
            if match:
                issues.append(match.group(0))
        label_match = REPORT_BULLET_RE.match(line)
        if label_match and payload:
            label = label_match.group(2).strip()
            allowed = allowed_by_category.get(category or "", set())
            if label not in allowed:
                issues.append(
                    f"unauthorized report label {label!r} in {category or 'unknown section'}"
                )
    return sorted(set(issues), key=str.lower)


def _collapse_punctuation(text: str) -> str:
    """Collapse duplicate punctuation without destroying markdown newlines."""
    cleaned = re.sub(r"[ \t]+,", ",", text)
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    cleaned = re.sub(r";\s*;+", "; ", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +(\n)", r"\1", cleaned)
    return cleaned


def merge_duplicate_report_bullets(text: str) -> str:
    """Combine multiple bullets that resolve to the same authoritative label."""
    category: str | None = None
    lines: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for line in text.splitlines():
        if line.startswith("## "):
            category = line[3:].strip()
        match = REPORT_BULLET_RE.match(line)
        if not match:
            lines.append(line)
            continue

        label = match.group(2).strip()
        key = (category or "", label)
        if key not in seen:
            seen[key] = len(lines)
            lines.append(line)
            continue

        description = match.group(4).strip()
        if description and not (len(description) > 1 and description[1].isupper()):
            description = description[0].lower() + description[1:]
        previous_index = seen[key]
        previous = lines[previous_index].rstrip().rstrip(".")
        combined = f"{previous}; {description}"
        if combined and not combined.endswith((".", "!", "?")):
            combined += "."
        lines[previous_index] = combined
    return "\n".join(lines)


def strip_gap_commentary(text: str) -> str:
    """Remove draft-review gap observations that should not appear in management reports."""
    cleaned = text
    for pattern in GAP_COMMENTARY_RES:
        cleaned = pattern.sub("", cleaned)
    cleaned = re.sub(r";\s*\.", ".", cleaned)
    cleaned = re.sub(r";\s*$", "", cleaned, flags=re.MULTILINE)
    return cleaned


def restore_markdown_structure(text: str) -> str:
    """Re-insert newlines when the skill returns a single-line markdown string."""
    cleaned = re.sub(r"\s*(##\s+)", r"\n\n\1", text)
    cleaned = re.sub(r"(?<=\.|\?|!)\s+(\*\s+\*\*)", r"\n\1", cleaned)
    cleaned = re.sub(r"\s+(\*\s+\*\*)", r"\n\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.lstrip("\n")


def enforce_authoritative_report_labels(
    markdown: str,
    payload: SynthesisInput,
) -> str:
    """Restore deterministic section labels when a section has one known initiative."""
    names_by_category: dict[str, set[str]] = defaultdict(set)
    for entry in payload.entries:
        names_by_category[entry.report_category].add(entry.report_name)

    category: str | None = None
    lines: list[str] = []
    bullet_re = re.compile(r"^(\s*\*\s+\*\*)([^*]+)(\*\*\s+-\s+.*)$")
    for line in markdown.splitlines():
        if line.startswith("## "):
            category = line[3:].strip()
        match = bullet_re.match(line)
        allowed = names_by_category.get(category or "", set())
        if match and len(allowed) == 1 and match.group(2).strip() not in allowed:
            line = f"{match.group(1)}{next(iter(allowed))}{match.group(3)}"
        lines.append(line)
    return "\n".join(lines)


def sanitize_report_markdown(markdown: str, payload: SynthesisInput | None = None) -> str:
    """Repair common synthesizer markdown issues to match format-status rules."""
    jira_phrases = build_jira_link_phrases(payload) if payload else {}
    github_phrases = build_github_link_phrases(payload) if payload else {}

    def replace_paren_jira(match: re.Match[str]) -> str:
        key = match.group(1)
        url = f"https://issues.redhat.com/browse/{key}"
        phrase = jira_phrases.get(key, "related work")
        return f"[{phrase}]({url})"

    def replace_paren_github(match: re.Match[str]) -> str:
        url = match.group(1)
        phrase = github_phrases.get(url)
        if phrase:
            return f"[{phrase}]({url})"
        if "/pull/" in url:
            pr_num = url.rstrip("/").split("/")[-1]
            return f"[PR #{pr_num}]({url})"
        return f"[change]({url})"

    cleaned = strip_inactive_backlog_clauses(markdown)
    cleaned = PAREN_JIRA_URL_RE.sub(replace_paren_jira, cleaned)
    cleaned = PAREN_GITHUB_URL_RE.sub(replace_paren_github, cleaned)
    cleaned = RAW_PR_LINK_RE.sub(
        lambda match: f"[{github_phrases.get(match.group(1), 'implementation change')}]"
        f"({match.group(1)})",
        cleaned,
    )
    cleaned = ADDITIONAL_EVIDENCE_SUFFIX_RE.sub("", cleaned)
    cleaned = limit_visible_github_links(cleaned)
    cleaned = GENERIC_EVIDENCE_SUFFIX_RE.sub("", cleaned)
    cleaned = SUPPORTING_PRS_SUFFIX_RE.sub("", cleaned)
    cleaned = EMPTY_MARKDOWN_LINK_RE.sub(r"\1", cleaned)
    cleaned = NOTES_SECTION_RE.sub("", cleaned)
    cleaned = strip_gap_commentary(cleaned)
    cleaned = restore_markdown_structure(cleaned)
    if payload:
        cleaned = enforce_authoritative_report_labels(cleaned, payload)
        cleaned = merge_duplicate_report_bullets(cleaned)
    cleaned = re.sub(r"^(## [^\n]+)\n(?!\n)", r"\1\n\n", cleaned, flags=re.MULTILINE)
    cleaned = _collapse_punctuation(cleaned)
    return cleaned.strip() + "\n"


def _person_display_name(session: Session, person_id: str) -> str:
    person = get_person(session, person_id)
    if person is not None:
        return person.display_name
    return person_id


def _missing_jira_keys_for_entries(entries: list[StatusEntry]) -> list[str]:
    missing: set[str] = set()
    for entry in entries:
        stored = set(((entry.extra or {}).get("evidence_labels") or {}).keys())
        for key in jira_keys_from_evidence(entry.evidence or []):
            if key not in stored:
                missing.add(key)
    return sorted(missing)


def _fetch_missing_issue_summaries(keys: list[str], settings: Settings) -> dict[str, str]:
    if not keys:
        return {}
    try:
        from status.collectors.jira import JiraCollectorError, fetch_jira_summaries

        return fetch_jira_summaries(keys, settings=settings)
    except JiraCollectorError as exc:
        log.warning("could not fetch Jira summaries for evidence labels: %s", exc)
        return {}
    except Exception as exc:  # noqa: BLE001 - report generation must survive enrichment failure
        log.warning("unexpected error fetching Jira summaries: %s", exc)
        return {}


def build_synthesis_input(
    session: Session,
    week_ending: date,
    *,
    settings: Settings | None = None,
) -> SynthesisInput:
    settings = settings or get_settings()
    entries = get_confirmed_entries_for_week(session, week_ending)
    participation = get_participation_for_week(session, week_ending)
    fetched_summaries = _fetch_missing_issue_summaries(
        _missing_jira_keys_for_entries(entries),
        settings,
    )

    synthesis_entries: list[SynthesisEntry] = []
    for entry in entries:
        if entry.state == "quiet":
            continue
        outcome = strip_inactive_backlog_clauses(entry.outcome)
        if not outcome:
            continue
        evidence_labels = merge_evidence_labels(
            (entry.extra or {}).get("evidence_labels"),
            entry.evidence or [],
            fetched_summaries,
            epic_key=entry.epic_key,
            epic_name=entry.epic_name_snapshot,
        )
        report_category, report_name = classify_report_entry(
            project=entry.project,
            epic_name=entry.epic_name_snapshot,
            outcome=outcome,
            evidence_labels=evidence_labels,
        )
        visible_evidence = select_visible_evidence(outcome, entry.evidence or [])
        visible_jira_keys = set(jira_keys_from_evidence(visible_evidence))
        visible_labels = {
            key: label
            for key, label in evidence_labels.items()
            if key in visible_jira_keys
        }
        synthesis_entries.append(
            SynthesisEntry(
                person_id=entry.person_id,
                display_name=_person_display_name(session, entry.person_id),
                project=entry.project,
                epic_key=entry.epic_key,
                epic_name=entry.epic_name_snapshot,
                state=entry.state,  # type: ignore[arg-type]
                outcome=outcome,
                blocker=entry.blocker,
                ask=entry.ask,
                evidence=visible_evidence,
                evidence_labels=visible_labels,
                report_category=report_category,
                report_name=report_name,
            )
        )

    synthesis_participation = [
        SynthesisParticipation(
            person_id=p.person_id,
            display_name=_person_display_name(session, p.person_id),
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


def default_report_filename(week_ending: date) -> str:
    return f"status-{week_ending.isoformat()}.md"


def build_dry_run_markdown(
    payload: SynthesisInput,
    week_ending: date,
    *,
    reason: str,
) -> str:
    """Render a ledger preview when the synthesizer skill is not invoked."""
    lines = [
        f"# {week_ending.strftime('%b %d, %Y')}",
        "",
        f"_{reason}. Ledger preview — use `--no-dry-run` after `status skills publish --skill synthesizer` for formatted output._",
        "",
    ]

    if not payload.entries:
        lines.append("_No confirmed entries for this week (quiet entries omitted)._")
        lines.append("")
    else:
        by_project: dict[str, list[SynthesisEntry]] = defaultdict(list)
        for entry in payload.entries:
            by_project[entry.report_category].append(entry)

        for category in ("Partner Enablement", "Certification / CI", "Mindshare"):
            if category not in by_project:
                continue
            lines.append(f"## {category}")
            lines.append("")
            for entry in sorted(
                by_project[category],
                key=lambda row: (row.report_name, row.display_name),
            ):
                lines.append(
                    f"* **{entry.report_name}** ({entry.display_name}) - {entry.outcome}"
                )
            lines.append("")

    participation_notices = {
        "expired": "No update from {name} this week.",
        "send_failed": "The draft status could not be delivered to {name}.",
        "collect_failed": "Status evidence collection failed for {name}.",
        "draft_failed": "Draft status generation failed for {name}.",
        "nudge_failed": "The status reminder could not be delivered to {name}.",
    }
    notices = sorted(
        (
            p.display_name,
            participation_notices[p.status].format(name=p.display_name),
        )
        for p in payload.participation
        if p.status in participation_notices
    )
    if notices:
        lines.append("## Team participation")
        lines.append("")
        for _name, notice in notices:
            lines.append(f"* {notice}")
        lines.append("")

    asks = [entry.ask for entry in payload.entries if entry.ask]
    if asks:
        lines.append("## Decisions needed")
        lines.append("")
        for ask in asks:
            lines.append(f"* {ask}")
        lines.append("")

    if payload.flags:
        lines.append("## Flags")
        lines.append("")
        for flag in payload.flags:
            lines.append(f"* {flag.message}")
        lines.append("")

    lines.append(
        f"_Input: {len(payload.entries)} entries, "
        f"{len(payload.participation)} participation rows, {len(payload.flags)} flags._"
    )
    return "\n".join(lines).rstrip() + "\n"


def _dry_run_output(
    payload: SynthesisInput,
    week_ending: date,
    *,
    reason: str = "Dry-run: synthesizer skill not invoked",
) -> SynthesisOutput:
    return SynthesisOutput(
        week_ending=week_ending.isoformat(),
        markdown=build_dry_run_markdown(payload, week_ending, reason=reason),
        sections_used=[],
        entries_cited=[
            f"{entry.person_id}:{entry.epic_key or 'unticketed'}" for entry in payload.entries
        ],
        non_responders=[
            p.display_name for p in payload.participation if p.status == "expired"
        ],
        asks=[entry.ask for entry in payload.entries if entry.ask],
    )


def run_synthesizer_from_payload(
    payload: SynthesisInput,
    week_ending: date,
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> SynthesisOutput:
    """Invoke the synthesizer skill without holding a database connection."""
    settings = settings or get_settings()

    if dry_run:
        return _dry_run_output(payload, week_ending)

    input_issues = synthesis_input_quality_issues(payload)
    if input_issues:
        raise RuntimeError(
            "management report input failed quality review: "
            f"{'; '.join(input_issues)}; edit and reconfirm the affected draft"
        )

    if not settings.synthesizer_skill_id:
        return _dry_run_output(
            payload,
            week_ending,
            reason="SYNTHESIZER_SKILL_ID is not set",
        )

    if skill_provider(settings) == "openai" and not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    if skill_provider(settings) != "openai" and not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    result = invoke_skill_json(
        skill_id=settings.synthesizer_skill_id,
        skill_version=settings.synthesizer_skill_version,
        payload=payload.model_dump(),
        instruction=SYNTHESIZER_INSTRUCTION,
        schema=SynthesisOutput,
        settings=settings,
        max_tokens=SYNTHESIZER_MAX_TOKENS,
    )
    assert isinstance(result, SynthesisOutput)
    markdown = sanitize_report_markdown(result.markdown, payload)
    quality_issues = management_quality_issues(markdown, payload)
    if quality_issues:
        phrases = ", ".join(repr(issue) for issue in quality_issues)
        raise RuntimeError(
            "management report contains vague placeholder language "
            f"({phrases}); recollect richer Jira descriptions/comments or edit and "
            "reconfirm the affected draft before delivery"
        )
    return result.model_copy(update={"markdown": markdown})


def run_synthesizer(
    session: Session,
    week_ending: date,
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> SynthesisOutput:
    payload = build_synthesis_input(session, week_ending)
    return run_synthesizer_from_payload(
        payload,
        week_ending,
        dry_run=dry_run,
        settings=settings,
    )


def synthesize_report(
    week_ending: date,
    *,
    dry_run: bool = False,
    persist: bool = False,
    output_path: Path | None = None,
    deliver: bool = False,
    settings: Settings | None = None,
) -> SynthesisOutput:
    """Run synthesizer and optionally persist, write file, and deliver to Slack."""
    from status.db import get_session

    settings = settings or get_settings()
    with get_session() as session:
        payload = build_synthesis_input(session, week_ending)

    # Skill calls can take minutes; do not keep a port-forwarded DB session open.
    result = run_synthesizer_from_payload(
        payload,
        week_ending,
        dry_run=dry_run,
        settings=settings,
    )

    if output_path is not None:
        output_path.write_text(result.markdown)

    delivered = False
    if deliver and not dry_run:
        if not settings.report_channel_id:
            raise RuntimeError("REPORT_CHANNEL_ID is not configured")
        if not settings.slack_bot_token:
            raise RuntimeError("SLACK_BOT_TOKEN is not configured")

        from status.slack.report import deliver_management_report

        deliver_management_report(
            settings.report_channel_id,
            result.markdown,
            week_ending=week_ending,
            bot_token=settings.slack_bot_token,
        )
        delivered = True

    if persist and not dry_run:
        with get_session() as session:
            confirmed_entries = get_confirmed_entries_for_week(session, week_ending)
            persist_report_run(
                session,
                week_ending,
                result,
                prompt_version=SYNTHESIZER_PROMPT_VERSION,
                model=settings.claude_model,
                confirmed_entries=confirmed_entries,
                output_uri=str(output_path) if output_path is not None else None,
                delivered=delivered,
            )

    return result
