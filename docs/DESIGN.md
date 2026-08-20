# Agentic Weekly Status Pipeline — Design Document

**Status:** Approved for implementation  
**Owner:** Yash Oza  
**Last updated:** 2026-08-19

---

## 1. Executive summary

Replace the current Google Form → Google Sheet → Apps Script → Claude skill workflow with an automated pipeline that:

1. **Collects** each engineer's week from Jira and GitHub
2. **Drafts** per-person status entries via a Claude Agent Skill
3. **Reviews** drafts in Slack (confirm, edit, or regenerate)
4. **Stores** confirmed entries in a Postgres ledger
5. **Synthesizes** a management report via a second skill (ported from `weekly_status-main`)
6. **Delivers** markdown (and optionally DOCX/email via existing `weekly_status` tooling)

**Core constraint:** A human confirms every line before it reaches management. Fully automated status generation produces plausible but unfalsifiable activity logs.

```
Jira + GitHub  ──▶  drafter skill  ──▶  Slack review  ──▶  ledger  ──▶  synthesis skill  ──▶  report
                    (per person)        (human confirms)   (Postgres)    (across team)
```

During the POC, the Google Form runs in parallel for at least three weeks to compare draft quality against ground truth.

---

## 2. Problem statement

### Current workflow

1. Engineers fill a Google Form every Friday: select project, name Jira epic, write free-text description (multiple entries allowed)
2. A Google Apps Script aggregates entries into a Google Sheet
3. Another Apps Script invokes a Claude skill that filters and formats entries
4. Output is published as a management report (markdown → DOCX → email)

### Pain points

| Problem | Impact |
|---|---|
| **Authoring from scratch** | Everyone reconstructs their week from memory; quality varies |
| **Prose in a Sheet** | Nothing accumulates; no quarter-over-quarter epic history |
| **One skill does two jobs** | Filtering and formatting cannot be tuned independently |
| **No gap detection** | Stale epics and unticketed work go unnoticed |
| **High Friday friction** | Form completion competes with end-of-week fatigue |

---

## 3. Proposed solution

Shift humans from **authoring** to **reviewing**.

| Stage | Who/what | Output |
|---|---|---|
| Collect | Python collectors (Jira + GitHub APIs) | Normalized JSON payload per person |
| Draft | `weekly-status-drafter` skill | Epic-grouped entries with evidence |
| Review | Slack DM (Looks right / Edit / Regenerate) | Confirmed or edited entries |
| Store | Postgres ledger | Append-only rows with audit chain |
| Synthesize | `weekly-status-synthesizer` skill | `status-YYYY-MM-DD.md` |
| Deliver | Slack channel + optional DOCX/email | Management report |

### Why Slack over Google Form

- Pre-filled drafts reduce Friday effort to ~3 minutes
- Contextual gap prompts ("Nothing here covers the partner sync on Tuesday…")
- Inline confirm/edit without leaving Slack
- Matches the UX mockup: status pills, flags, interactive buttons

### Why two skills

| Skill | Scope | Failure mode |
|---|---|---|
| **Drafter** | One person, one week | Wrong epic grouping, invented claims |
| **Synthesizer** | Whole team, one week | Wrong section, dropped asks, bad formatting |

Separating them allows independent prompt tuning and version pinning.

---

## 4. Relationship to existing repos

### `files/` — POC design artifacts (source of truth for v1)

- `DESIGN.md` — original design notes (superseded by this document)
- `SKILL.md` — drafter skill definition
- `ledger_schema.sql` — Postgres DDL
- `skills_client.py` — Skills API client

### `weekly_status-main/` — current production synthesis (Claude Code commands)

| File | Role in new pipeline |
|---|---|
| `.claude/commands/format-status.md` | **Port** → `skills/weekly-status-synthesizer/SKILL.md` |
| `status template.md` | **Port** → output structure and examples in synthesizer skill |
| `.claude/commands/export-docx.md` | **Keep** — Phase 2, post-synthesis |
| `.claude/commands/draft-status-emails.md` | **Keep** — Phase 2, post-synthesis |
| `.claude/commands/extract-status.md` | **Drop** — replaced by collector + drafter + ledger |
| `.claude/agents/vp-engineering.md` | **Optional** — wrap synthesizer output post-M5 |

The synthesizer skill preserves all formatting and categorization rules from `format-status.md`. Only the **input contract** changes (ledger JSON instead of extracted Google Doc text).

---

## 5. Architecture

Five runtime components, each invocable from the CLI.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  collector  │────▶│   drafter   │────▶│  slackbot   │
│ Jira+GitHub │     │ Claude Skill│     │ Socket Mode │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │ confirm
                                               ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  scheduler  │◀────│ synthesizer │◀────│   ledger    │
│  CronJobs   │     │ Claude Skill│     │  Postgres   │
└─────────────┘     └─────────────┘     └─────────────┘
```

### 5.1 Collector

**Responsibility:** Fetch raw signals for one person and one week; normalize into the drafter skill input payload.

**Jira signals:**
- Issues where person was assignee or commenter with activity in the window
- Transitions with timestamps, comments, epic link, `in_progress_since`

**GitHub signals:**
- PRs authored by person, merged or opened in the window
- Linked issue keys parsed from title and body

**Context:**
- Last 3 weeks of confirmed `status_entry` rows for pace/staleness detection

**Output:** JSON matching drafter input contract. Save to `fixtures/` with `--save-fixture`.

**Behaviors:**
- Never fail the whole run because one person's Jira query errored
- Rate-limit against Jira
- Per-person failures → `participation.status = 'send_failed'`

### 5.2 Drafter

**Responsibility:** Invoke drafting skill, validate output, persist draft rows.

- Skill: `skills/weekly-status-drafter/SKILL.md`
- Validate JSON against Pydantic schema before persisting
- On validation failure: retry once, then empty draft with flag
- Persist to `status_entry` with `source = 'drafted'`, `confirmed_at = NULL`
- Write gap-detection flags to `flag` table

**Behaviors:**
- Idempotent per (person, week): re-run replaces unconfirmed drafts only
- Record `prompt_version` on every row

### 5.3 Slackbot

**Responsibility:** Deliver drafts; capture confirmations and edits.

Long-lived Bolt app via Socket Mode (no ingress).

**Message shape:**
- Read-only draft rendering with status pills (`shipped`, `slipped`, `quiet`, etc.)
- Buttons: **Looks right** (primary), **Edit**, **Regenerate**
- Gap flags as highlighted banners

**Looks right:** Mark entries confirmed, update message to compact confirmed state.

**Edit:** Modal with one field per epic, drop checkboxes, unticketed work field, leadership asks field. Changed entries → `source = 'drafted_edited'` with new revision.

**Regenerate:** Modal with reason select; re-run drafter; replace message.

**Slash command:** `/status` pulls current week's draft on demand.

**Critical Slack constraints:**
1. Ack interactivity within 3 seconds; work in background task
2. `views.open` before DB reads (`trigger_id` expires in 3s)
3. Modal block limit ~100 (~20 epics max; cap and note remainder)
4. `chat.update`, never threaded replies

### 5.4 Synthesizer

**Responsibility:** Produce management report from confirmed ledger entries.

- Reads `status_entry` where `is_current` and `confirmed_at IS NOT NULL`
- Includes `participation` rows and unacknowledged `flag` rows
- Invokes `skills/weekly-status-synthesizer/SKILL.md`
- Writes `report_run` + `report_entry` join rows (audit chain)
- Delivers markdown to private Slack channel

**Synthesis output sections** (from `weekly_status-main`):
- Partner Enablement
- Certification / CI
- Mindshare

**Synthesis rules preserved from `format-status.md`:**
- Categorization: partner work vs cert-tool work vs mindshare
- Format: `* **Name** - description` (inline bold, no `###` per entry)
- No raw Jira IDs in visible text — links on noun phrases only
- Semicolons followed by lowercase
- Alphabetical sort within sections
- Exclude Learning, PTO/No Status, Missing Status
- PR abbreviated as `PR #N`

**New synthesis requirements** (from ledger):
- Organize by epic/project, not by person
- Non-responders rendered explicitly ("no update from X this week")
- Every `ask` from ledger appears in decisions section verbatim or near-verbatim
- Omit rather than pad quiet projects

### 5.5 Scheduler

OpenShift CronJobs:

| Job | Schedule | Action |
|---|---|---|
| `collect-and-draft` | Fri 08:30 | collector + drafter for all active people |
| `send-drafts` | Fri 09:00 | slackbot sends DMs |
| `nudge` | Fri 14:00 | remind unconfirmed |
| `lock-and-report` | Mon 09:00 | expire unconfirmed, run synthesizer, deliver |

**Cutoff:** At Monday 09:00, mark unconfirmed participation `expired` and generate from what exists. Do not block on stragglers.

---

## 6. Data model

Full DDL: `ledger_schema.sql`. Key decisions:

| Decision | Rationale |
|---|---|
| Grain: one row per (person, epic, week_ending) | Matches how managers think |
| Append-only revisions | Reports reproducible as of send date |
| `draft_outcome` alongside `outcome` | Delta = drafting prompt quality signal |
| `participation` separate from entries | Disambiguates PTO vs bot failure vs quiet week |
| Epic name snapshotted | Epics get renamed; history stays coherent |
| `report_entry` join table | Audit chain: report → entry → evidence → Jira |

Migrations via Alembic.

---

## 7. Skill contracts

### 7.1 Drafter input

```json
{
  "person": "string",
  "week_start": "YYYY-MM-DD",
  "week_end": "YYYY-MM-DD",
  "jira_issues": [{ "key": "...", "summary": "...", "epic_key": "...", "transitions": [], "comments": [] }],
  "pull_requests": [{ "url": "...", "title": "...", "linked_issue_keys": [] }],
  "previous_entries": []
}
```

### 7.2 Drafter output

```json
{
  "person": "string",
  "week_ending": "YYYY-MM-DD",
  "entries": [{
    "project": "string",
    "epic_key": "string | null",
    "epic_name": "string | null",
    "state": "shipped | progressing | slipped | blocked | quiet",
    "outcome": "one sentence, past tense",
    "evidence": ["AIPLAT-231", "https://github.com/..."],
    "blocker": "string | null",
    "ask": "string | null",
    "confidence": "high | medium | low",
    "needs_human": true,
    "why_flagged": "string | null"
  }],
  "flags": ["string"],
  "unticketed_prompt": "string"
}
```

### 7.3 Synthesizer input

```json
{
  "week_ending": "YYYY-MM-DD",
  "entries": [{
    "person_id": "string",
    "display_name": "string",
    "project": "string",
    "epic_key": "string | null",
    "epic_name": "string | null",
    "state": "shipped | progressing | slipped | blocked | quiet",
    "outcome": "string",
    "blocker": "string | null",
    "ask": "string | null",
    "evidence": ["EET-4853", "https://github.com/..."]
  }],
  "participation": [{ "person_id": "string", "display_name": "string", "status": "confirmed | expired | on_leave" }],
  "flags": [{ "message": "string", "person_id": "string | null", "epic_key": "string | null" }]
}
```

### 7.4 Synthesizer output

Markdown file matching `status template.md` structure:

```markdown
# Aug 14, 2026

## Partner Enablement

* **IBM** - Further discussions regarding [Spectrum Symphony Operator](https://issues.redhat.com/browse/EET-4853)...

## Certification / CI

* **Chart-Verifier** - Released [Chart Verifier 1.14.0](https://github.com/...)...

## Mindshare

* **Upstream Open Source Leadership** - Opened a PR to remove [unnecessary loops](https://github.com/...)...
```

Plus optional metadata block (not in final report):

```json
{
  "week_ending": "2026-08-14",
  "sections_used": ["Partner Enablement", "Certification / CI"],
  "entries_cited": ["uuid", "uuid"],
  "non_responders": ["bob"],
  "asks": ["Needs a call on which scheduler we standardize on."]
}
```

---

## 8. Skills integration

Both skills are Claude Agent Skills uploaded to the workspace and invoked via Messages API `container` parameter with code execution tool.

**Constraints:**
- No network inside skill container — collector assembles payload externally
- No runtime package installation — stdlib only in bundled scripts
- Version pinning in deployed config (`skill_id@version`); `latest` for local dev
- Output validated by Pydantic; on failure → empty draft with flag, never silent repair

**Client:** `src/status/skills/client.py` (from `files/skills_client.py`)

**Development loop:**
1. Edit `skills/*/SKILL.md`
2. Test locally in Claude Code against fixtures
3. `status skills publish --skill drafter`
4. Pin version in config; diff output against previous version on same fixtures

---

## 9. Repository layout

```
agentic-status-report/
  docs/
    DESIGN.md                 # this document
  skills/
    weekly-status-drafter/
      SKILL.md
    weekly-status-synthesizer/
      SKILL.md
  src/status/
    __init__.py
    config.py
    cli.py
    db/
      models.py
      repo.py
    collectors/               # M1
      jira.py
      github.py
      payload.py
    skills/
      client.py
      drafter.py              # M2
      synthesizer.py          # M5
      schemas.py
    slack/                    # M3-M4
      app.py
      blocks.py
      handlers.py
  alembic/
    versions/
  fixtures/
  tests/
  deploy/
    cronjobs.yaml
    deployment.yaml
    secrets.example.yaml
  weekly_status-main/         # reference; synthesis rules ported to skill
  files/                      # original POC artifacts
  pyproject.toml
  README.md
```

**CLI commands (all support `--dry-run`):**

```bash
status collect --person yash --week 2026-08-14 --save-fixture
status draft   --fixture fixtures/yash-2026-08-14.json --dry-run
status send    --person yash --week 2026-08-14
status report  --week 2026-08-14 --dry-run
status skills publish --skill drafter
status skills list
```

---

## 10. Configuration

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection |
| `ANTHROPIC_API_KEY` | Skill invocation |
| `SLACK_BOT_TOKEN` | `xoxb-…` |
| `SLACK_APP_TOKEN` | `xapp-…`, Socket Mode |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Jira Cloud |
| `GITHUB_TOKEN` | PR data; read-only |
| `REPORT_CHANNEL_ID` | Weekly report destination |
| `PILOT_PERSON_IDS` | Comma-separated allowlist (POC safety rail) |
| `DRAFTER_SKILL_ID` / `DRAFTER_SKILL_VERSION` | Pinned in prod |
| `SYNTHESIZER_SKILL_ID` / `SYNTHESIZER_SKILL_VERSION` | Pinned in prod |
| `CLAUDE_MODEL` | Default `claude-sonnet-5` |

---

## 11. Milestones

| # | Milestone | Done when |
|---|---|---|
| M0 | Scaffolding | `status --help` works; schema applies to empty DB |
| M1 | Collector | Real person's week matches human judgment; 5+ fixtures |
| M2 | Drafter | Valid entries from fixtures; idempotent re-runs |
| M3 | Slack confirm | One person confirms; ledger shows `confirmed_at` |
| M4 | Edit/regenerate | Edits create revisions; originals preserved |
| M5 | Synthesizer | Report posted; audit chain populated |
| M6 | Scheduling | Full week unattended; broken token alerts |

**M1 prerequisite:** Run `daily-summary` plugin for one week; output must be recognizable. If thin, fix Jira hygiene before building collector.

---

## 12. Success criteria (after 4 weeks)

- ≥70% of drafted entries confirmed without edit
- Median confirm time under 3 minutes
- ≥80% confirm before Monday cutoff without chase
- Manager judges report ≥ as useful as current one
- Zero untraceable claims in report

---

## 13. POC scope

### In scope
- One pilot team (6–10 people)
- Jira + GitHub signals
- Slack DM review
- Postgres ledger
- Weekly synthesis to Slack channel
- Manual CLI trigger for every stage

### Out of scope (Phase 2)
- Calendar / Google Docs signals
- Google Sheet mirroring
- DOCX export and email distribution (reuse `weekly_status-main` commands)
- VP Engineering review subagent
- Multi-team routing
- Historical backfill from Sheet data

### Explicit non-goals
- Do not remove Google Form during POC
- Do not fully automate without human confirmation

---

## 14. Risks

| Risk | Mitigation |
|---|---|
| Jira hygiene (Friday batch updates, stale In Progress) | M1 compares payloads to Form entries; surface before M2 |
| Empty `ask` fields | Dedicated Slack field in edit modal, not more prompt engineering |
| Slack handler timeouts | Ack first; background tasks; idempotent retries |
| Skill version drift | Pin versions in deployed config; CI publishes on `skills/**` merge |
| Draft hallucination | Evidence required; validation rejects ungrounded claims |

---

## 15. Open questions

1. Which pilot team? Manager agrees to parallel reports for 3 weeks?
2. Existing Postgres instance or provision new?
3. Jira service account vs per-person tokens?
4. Report to Slack channel or email?

---

## 16. Testing strategy

- **Fixtures over live calls** — scrub PII before commit
- **Golden tests on JSON shape**, not prose wording
- **Unit tests on revision logic** (`is_current` flip, unique constraints)
- **Manual test set before M3:** normal week, meeting-heavy week, messiest Jira on team

---

## 17. Conventions

- Conventional commits scoped to component: `feat(collector):`, `fix(slack):`
- Type hints throughout; `mypy --strict` on `src/status/`
- No secrets in repo
- Skill prompts versioned in `skills/`, not strings in Python

---

## 18. References

- `files/SKILL.md` — drafter skill (v1)
- `files/ledger_schema.sql` — Postgres DDL
- `files/skills_client.py` — Skills API client
- `weekly_status-main/.claude/commands/format-status.md` — synthesis rules source
- `weekly_status-main/status template.md` — report structure
- [Claude Skills API guide](https://platform.claude.com/docs/en/build-with-claude/skills-guide)
