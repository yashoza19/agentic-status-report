# Weekly status pipeline — POC design doc

Status: draft, for implementation
Audience: Claude Code, and reviewers on the team

---

## 1. Problem

Weekly engineering status is collected through a Google Form. Each person selects
a project, names a Jira epic, and writes a free-text description of the week.
Entries land in a Google Sheet; an Apps Script invokes a Claude skill that filters
and formats them into a report sent to management.

Three problems with this:

1. **Authoring from scratch is expensive.** Everyone reconstructs their week from
   memory on Friday afternoon. Quality varies by how tired they are.
2. **The intermediate store is prose in a Sheet.** Nothing accumulates. There is
   no way to ask "what happened on this epic last quarter" without rereading
   twelve reports.
3. **One skill does filtering and formatting.** The two jobs have different
   failure modes and cannot be tuned independently.

## 2. Approach

Shift the human from **authoring** to **reviewing**. A scheduled job pulls each
person's actual week from Jira and GitHub, a skill drafts entries grouped by
epic, and the person confirms or corrects them in Slack. Confirmed entries land
in a structured ledger. A separate skill synthesizes the management report from
the ledger.

The core constraint: **a human confirms every line before it reaches management.**
Fully automated status generation produces plausible, unfalsifiable activity logs.
The point of the human step is not approval theatre — it is the only thing
keeping the report tied to reality.

```
Jira + GitHub  ──▶  drafter skill  ──▶  Slack review  ──▶  ledger  ──▶  synthesis skill  ──▶  report
                    (per person)        (human confirms)   (Postgres)    (across people)
```

## 3. POC scope

### In scope

- One pilot team (assume 6–10 people)
- Signals: Jira issue activity, GitHub PR activity
- Slack DM with confirm / edit / regenerate
- Postgres ledger, append-only with revisions
- Weekly synthesis into a markdown report posted to a private Slack channel
- Manual CLI trigger for every stage, in addition to scheduled runs

### Out of scope for the POC

- Calendar or Google Docs signals
- Google Sheet mirroring (revisit after the ledger proves out)
- Multi-team routing or per-manager report variants
- Any UI beyond Slack
- Auth beyond a single Slack workspace and service-account tokens
- Historical backfill from existing Sheet data

### Explicit non-goals

- Do not remove the Google Form during the POC. Run in parallel for at least
  three weeks so we can compare drafted entries against what people actually
  wrote. If we cut over immediately we lose the only ground truth we have.

## 4. Success criteria

The POC succeeds if, after four weeks:

- ≥70% of drafted entries are confirmed without edit (`source = 'drafted'`)
- Median time from DM to confirmation is under 3 minutes
- ≥80% of the pilot team confirm before Monday cutoff without a manual chase
- The generated report is judged by the pilot team's manager as at least as
  useful as the current one
- Zero instances of a claim in the report that cannot be traced to a Jira key
  or PR via `report_entry → status_entry → evidence`

The first metric is the important one. If humans are rewriting most drafts, the
drafting prompt is wrong and the whole premise needs revisiting rather than
more engineering.

## 5. Architecture

Five components. Each runs independently and is invocable from the CLI.

### 5.1 `collector`

**Responsibility:** fetch raw signals for one person and one week, normalize into
the drafter skill's input payload.

- Jira: issues where the person was assignee or commenter with activity in the
  window. Include transitions with timestamps, comments, epic link, and
  `in_progress_since`.
- GitHub: PRs authored by the person, merged or opened in the window, with
  linked issue keys parsed from title and body.
- Previous entries: last 3 weeks of `status_entry` rows for the person, so the
  skill can judge pace and detect staleness.

**Output:** a JSON payload matching the input contract in `SKILL.md`. Write it to
disk under `fixtures/` when run with `--save-fixture` so later stages can be
tested without hitting external APIs.

**Key behaviors:**
- Never fail the whole run because one person's Jira query errored. Collect
  per-person, record failures in `participation.status = 'send_failed'`.
- Rate-limit against Jira. Assume a shared instance with other consumers.

### 5.2 `drafter`

**Responsibility:** invoke the drafting skill with a collected payload, validate
the output, persist draft rows.

- Skill definition lives at `skills/weekly-status-drafter/SKILL.md` (already
  written — see attached file, treat it as the starting point and expect to
  iterate on the examples section with real tickets).
- Validate the returned JSON against a schema before persisting. On validation
  failure, retry once, then fall back to an empty draft with a flag rather than
  persisting garbage.
- Persist to `status_entry` with `source = 'drafted'`, `confirmed_at = NULL`,
  `draft_outcome = outcome`, and the current `prompt_version`.
- Write gap-detection flags to `flag`.

**Key behaviors:**
- Idempotent per (person, week). Re-running replaces unconfirmed drafts; it must
  never overwrite a confirmed entry.
- Record `prompt_version` on every row. Without it we cannot tell whether a
  prompt change improved anything.

### 5.3 `slackbot`

**Responsibility:** deliver drafts, capture confirmations and edits.

Long-lived process. Socket Mode, so no ingress and no request-signature
verification needed.

**Message shape:** read-only rendering of the draft, then three buttons:
`Looks right` (primary), `Edit`, `Regenerate`.

- `Looks right` → mark all entries `confirmed_at = now()`, `source` unchanged,
  `participation.status = 'confirmed'`, then `chat.update` the message into a
  compact confirmed state.
- `Edit` → `views.open` a modal with one `plain_text_input` per epic pre-filled
  with the drafted outcome, a checkbox to drop each entry, plus two free-text
  fields at the bottom: unticketed work, and "anything you need from leadership?"
  On submit, insert new revisions for changed entries with
  `source = 'drafted_edited'`, leave unchanged ones as `drafted`.
- `Regenerate` → open a small modal with a reason select (wrong epic grouping /
  missed work / wrong tone / other), record it in
  `participation.regenerate_reason`, re-run drafter, replace the message.

**Critical constraints, in order of how likely they are to bite:**

1. **Ack within 3 seconds.** Every interactivity payload needs a response inside
   3s or Slack retries and you get duplicate submissions. Ack first, do work in
   a background task. Make handlers idempotent on Slack's retry headers anyway.
2. **`trigger_id` expires in 3 seconds.** The `Edit` handler must call
   `views.open` before any database read. Open a skeleton modal, then
   `views.update` once the data is loaded.
3. **Modal block limit is 100.** At ~4 blocks per epic that caps around 20
   epics. Cap the modal at the top N by activity and surface the remainder as a
   note rather than silently truncating.
4. **`chat.update`, never a threaded reply.** Otherwise the DM accumulates and
   people stop reading it.

Also expose a `/status` slash command that pulls up the current week's draft on
demand, so someone who dismissed the DM has a way back in.

### 5.4 `synthesizer`

**Responsibility:** produce the management report from confirmed entries.

- Reads all `status_entry` rows for the week where `is_current` and
  `confirmed_at IS NOT NULL`, plus `participation` rows and unacknowledged `flag`
  rows.
- Invokes a second skill (`skills/weekly-status-synthesizer/SKILL.md`, **to be
  written as part of M5** — see requirements below).
- Writes a `report_run` row and one `report_entry` row per entry cited. The
  join table is the audit chain; populate it even though nothing reads it yet.
- Delivers as markdown to a private Slack channel.

**Requirements for the synthesis skill:**
- Sections: what shipped, what slipped and why, decisions needed, risks.
- Organized by epic and project, not by person. Managers track initiatives.
- Non-responders rendered explicitly ("no update from X this week"), never
  silently omitted — silent omission is how someone disappears from three
  consecutive reports unnoticed.
- Omit rather than pad. A quiet project gets one line or no line.
- Every `ask` from the ledger must appear in "decisions needed" verbatim or
  near-verbatim. Asks are the highest-value content and the easiest to smooth away.

### 5.5 `scheduler`

OpenShift CronJobs:

| Job | Schedule | Action |
|---|---|---|
| `collect-and-draft` | Fri 08:30 | collector + drafter for all active people |
| `send-drafts` | Fri 09:00 | slackbot sends DMs |
| `nudge` | Fri 14:00 | remind unconfirmed |
| `lock-and-report` | Mon 09:00 | expire unconfirmed, run synthesizer, deliver |

Cutoff behavior matters: **do not block report generation on stragglers.** At
Monday 09:00, mark unconfirmed participation rows `expired` and generate from
what exists.

## 6. Data model

Full DDL in `ledger_schema.sql` (attached). Summary of the decisions encoded there:

- **Grain:** one current row per (person, epic, week_ending) in `status_entry`.
- **Append-only:** corrections insert a new revision and flip `is_current` on the
  old row. Never update in place — reports must be reproducible as of the date
  they were sent.
- **`draft_outcome` alongside `outcome`:** the delta is the only quality signal
  for the drafting prompt.
- **`participation` separate from content:** a missing entry row is ambiguous
  (PTO? quiet week? bot broke?). Participation disambiguates.
- **Epic name snapshotted, epic key joined:** epics get renamed and historical
  rollups otherwise look like two different initiatives.
- **`report_entry` join table:** report → entries → evidence → Jira. This is how
  we answer "where did this claim come from" months later.

Migrations: use Alembic. The schema will change during the POC; make that cheap
from day one rather than hand-editing tables.

## 6a. Skills: development and integration

Both prompts are implemented as Claude Agent Skills — custom Skills uploaded to
the workspace and invoked via the Messages API `container` parameter. Reference
implementation of the client layer is in `skills_client.py` (attached); it
belongs at `src/status/skills/client.py`.

### Why Skills rather than system prompts

A plain system prompt would work today. Skills are chosen for three reasons
that matter over the life of this project: versioning is native and maps
directly onto the ledger's `prompt_version` column; the same definition is
usable from Claude Code and claude.ai while iterating; and progressive
disclosure gives us somewhere to put a project glossary, epic taxonomy, and
per-team tone examples without inflating every request.

### Constraints that shape the design

1. **Skills require the code execution tool** and run in a sandboxed container.
2. **No network access inside the container.** The skill cannot call Jira or
   GitHub. This is why `collector` is a separate component — the skill only ever
   sees an assembled payload.
3. **No runtime package installation.** Only pre-installed packages are
   available. Any helper script bundled with a skill must use stdlib.
4. **Custom Skills are workspace-scoped**, not user- or session-scoped. Any API
   key in the workspace can read, invoke, and delete them. Use a dedicated
   workspace if that isolation matters.
5. **A new version is a full snapshot, not a delta.** Re-upload the entire
   directory each time; omitted files are not carried forward, and the `name`
   in SKILL.md must match the existing skill's name.
6. **Limits:** 20 Skills per request, 30 MB per upload, frontmatter `name`
   ≤64 chars (lowercase, numbers, hyphens; no reserved words), `description`
   ≤1024 chars.

### Development loop

```
1. Edit skills/weekly-status-drafter/SKILL.md
2. Test locally in Claude Code against a saved fixture:
     "Use the weekly-status-drafter skill on fixtures/pilot-2026-08-14.json"
   Iterate here — no upload, no version churn, fastest feedback.
3. status skills publish --skill drafter
     -> uploads the directory, returns a version id
4. Pin the version in config, run:
     status draft --fixture … --dry-run
5. Compare output against the previous version's output on the same fixtures
   before pinning it in the deployed config.
```

Step 5 is the one to not skip. Fixtures plus a diff are the entire evaluation
strategy for the POC — there is no point building anything more elaborate until
we know whether the drafts are usable at all.

### Version pinning policy

- **Deployed config pins an explicit version id.** With `latest`, anyone
  publishing to the workspace changes what the Friday job runs, mid-week, with
  no deploy and no signal.
- **Local development uses `latest`.**
- `SkillRef.prompt_version` (`skill_id@version`) is what gets stamped onto every
  `status_entry` and `report_run` row.

### Output handling

Skills return JSON as text; the client strips fences and validates against a
pydantic model before anything is persisted. On validation failure the client
raises rather than repairing — `drafter` then persists an empty draft with a
flag. A human writing their own entry is a recoverable outcome; a silently
repaired draft reaching management is not.

Note for later: Skills can also write output files in the container and return
`file_id`s for download via the Files API. Not needed at this payload size, but
it is the escape hatch if the synthesizer's report ever outgrows a text
response. Structured outputs may also be usable to enforce the schema at the
API level — worth checking compatibility with code execution before relying on it.

### CI

A GitHub Action on merge to `main` that touches `skills/**` should publish a new
version of the affected skill and open a PR bumping the pinned version in
deploy config. Publishing and deploying stay separate steps deliberately.

## 6b. Prior art: redhat-community-ai-tools/claude-plugins

https://github.com/redhat-community-ai-tools/claude-plugins is a Red Hat
community marketplace of Claude Code plugins. Several overlap with this
pipeline and should be mined before writing prompts from scratch.

**These are Claude Code plugins, not API Skills.** They execute by shelling out
to `jira`, `gh`, and `git`. The API Skills container has no network access and
cannot install packages, so none of them run in our pipeline unmodified. We take
their content, not their code.

### What to port from where

| Source | Port into | What |
|---|---|---|
| `daily-summary` | drafter SKILL.md | Commit → PR → ticket collapsing; epic grouping; the "ask the user if no work found" rule |
| `daily-summary` | `slack/blocks.py` | Slack link syntax `<url\|text>` for Jira keys — markdown does not render in Slack. Red Hat custom emoji codes (`:merged2:`, `:review:`, `:in-progress:`) |
| `daily-summary` | collectors | Multi-repo `gh pr list` loop pattern |
| `jira` → `generate-status-report` | synthesizer SKILL.md | Audience segmentation (executive / team / standup); the several-targeted-JQL-queries pattern rather than one broad query |
| `jira-mcp` → `jira-mcp-server` | `collectors/jira.py` | Reference implementation for Red Hat Jira auth. We do **not** use the MCP server itself |
| `quarterly-connection` | later | Aggregates Jira + GitHub + Google Workspace into Red Hat QC format, with cycle-time analysis. Once the ledger has a quarter of data it may be a better consumer of it than anything we would build |

`daily-summary` is hardcoded to the OSAC project, five named repos, and one
contributor's local memory path. Assume most of it needs parameterizing.

**Why not `jira-mcp`:** its own README scopes it to interactive natural-language
workflows and batch operations with decision logic. Our Friday job is a
deterministic, read-only, scheduled query. An MCP server adds a moving part and
non-determinism for no gain.

**Why not the Claude Agent SDK runtime:** running Claude Code as a library would
let these plugins execute natively. Rejected because we need validated JSON
persisted on a schedule — an agent loop with shell and filesystem access is
harder to test, harder to make idempotent, and produces output we cannot cleanly
diff across prompt versions.

### Pre-M1 validation task

Before writing pipeline code, install `daily-summary` in Claude Code and use it
for a week:

```
claude plugin marketplace add https://github.com/redhat-community-ai-tools/claude-plugins
claude plugin install daily-summary
```

This is the cheapest available test of the design's biggest risk — whether Jira
and GitHub carry enough signal to draft from at all. If the summaries are
recognizably the week that happened, M1 will work. If they are thin, the answer
is Jira hygiene, not more engineering.

### Security note

If we vendor any of these skills, pin a commit rather than tracking `main`, and
run the repo's own `skill-scanner` plugin against anything we adopt. Third-party
prompt content that feeds a report going to management is a supply chain we
should treat like any other.

## 7. Repo layout

```
weekly-status/
  README.md
  pyproject.toml
  alembic/
    versions/
  skills/
    weekly-status-drafter/SKILL.md
    weekly-status-synthesizer/SKILL.md
  src/status/
    config.py            # env parsing, typed settings
    db/
      models.py          # SQLAlchemy models mirroring ledger_schema.sql
      repo.py            # query helpers; all revision logic lives here
    collectors/
      jira.py
      github.py
      payload.py         # normalization into the skill input contract
    skills/
      client.py          # upload, versioning, invocation (see skills_client.py)
      drafter.py         # payload -> validated draft entries
      synthesizer.py     # entries -> report
      schemas.py         # pydantic models for skill output validation
    slack/
      app.py             # Bolt app, Socket Mode
      blocks.py          # message and modal construction
      handlers.py
    cli.py               # typer entrypoints for every stage
  fixtures/              # saved payloads for offline testing
  tests/
  deploy/
    cronjobs.yaml
    deployment.yaml      # the long-lived bot
    secrets.example.yaml
```

Every stage must be runnable standalone:

```
status collect --person pilot --week 2026-08-14 --save-fixture
status draft   --fixture fixtures/pilot-2026-08-14.json --dry-run
status send    --person pilot --week 2026-08-14
status report  --week 2026-08-14 --dry-run

status skills publish --skill drafter        # new version, prints version id
status skills list                           # workspace custom skills
```

`--dry-run` prints what would happen and persists nothing. This is not optional
polish; without it every test costs Jira calls and tokens.

## 8. Configuration

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection |
| `ANTHROPIC_API_KEY` | skill invocation |
| `SLACK_BOT_TOKEN` | `xoxb-…` |
| `SLACK_APP_TOKEN` | `xapp-…`, Socket Mode |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Jira Cloud |
| `GITHUB_TOKEN` | PR data; read-only scope |
| `REPORT_CHANNEL_ID` | where the weekly report lands |
| `PILOT_PERSON_IDS` | comma-separated allowlist; hard gate for the POC |
| `DRAFTER_SKILL_ID` / `DRAFTER_SKILL_VERSION` | pinned in deployed config, `latest` locally |
| `SYNTHESIZER_SKILL_ID` / `SYNTHESIZER_SKILL_VERSION` | as above |
| `CLAUDE_MODEL` | defaults to `claude-sonnet-5` for the batch jobs |

`PILOT_PERSON_IDS` is a safety rail — it should be impossible to accidentally DM
the whole org during the POC.

**Slack app scopes:** `chat:write`, `im:write`, `users:read`, `commands`,
`connections:write`.

## 9. Milestones

Each milestone is independently demoable. Do not start the next until the
acceptance criteria pass.

### M0 — scaffolding
Repo, config, Alembic, schema applied, CLI skeleton, `--dry-run` plumbed through.
**Done when:** `status --help` lists every stage and the schema applies cleanly
to an empty database.

### M1 — collector
Jira and GitHub clients, payload normalization, fixture saving.
**Prerequisite:** the `daily-summary` trial in §6b has run for at least a week
and the output was judged recognizable. If it was not, stop and fix Jira hygiene
before building the collector.
**Done when:** `status collect` produces a payload for a real person's real week
that a human agrees reflects what they actually did, and at least five fixtures
covering different people and weeks are saved for regression diffing.

### M2 — drafter
Skill invocation, output validation, persistence, gap flags.
**Done when:** running against three saved fixtures produces valid entries, and
a re-run does not duplicate or clobber rows.

### M3 — Slack send and confirm
Bot process, DM rendering, `Looks right` path only.
**Done when:** one person receives a draft, taps confirm, and the ledger shows
confirmed entries with a populated `participation` row.

### M4 — edit and regenerate
Modal, per-entry edits, drop checkboxes, leadership-asks field, regenerate with
reason capture.
**Done when:** an edited entry creates a revision with `source =
'drafted_edited'` and the original row survives with `is_current = false`.

### M5 — synthesizer
Second skill written, report generation, `report_entry` population, delivery.
**Done when:** a report is posted to the channel and every claim in it traces to
a `status_entry` row via the join table.

### M6 — scheduling and observability
CronJobs, nudges, cutoff, structured logging, a heartbeat alert if the Friday
draft job does not complete.
**Done when:** a full week runs unattended and a deliberately broken Jira token
produces an alert rather than silence.

## 10. Testing

- **Fixtures over live calls.** Save real payloads once, replay in tests. Scrub
  names and ticket content before committing anything.
- **Golden tests on skill output shape, not wording.** Assert the JSON validates,
  that every entry has non-empty evidence, that epic grouping collapses
  correctly. Do not assert on generated prose; it will change and the test will
  become noise you ignore.
- **Revision logic gets real unit tests.** The `is_current` flip and the unique
  constraint are where silent data corruption will come from.
- **Manual test set before M3 ships:** run the drafter against three real weeks —
  a normal week, a week where someone was mostly in meetings, and the messiest
  Jira on the team. The third is the actual test.

## 11. Risks and open questions

**Jira hygiene is a hard dependency.** If people update tickets in a Friday
batch or leave things In Progress for months, drafts will be wrong in ways that
look right. M1 should surface this before we build further — compare a few
collected payloads against what people wrote in the Form.

**The `ask` field will probably stay empty.** Engineers rarely write "I need a
decision on X" in a ticket comment. If it is empty after two weeks, the fix is
the dedicated Slack field, not more prompt engineering.

**Visibility changes behavior.** DM-only for the POC. A shared channel creates
useful ambient awareness but turns status into performance. Easy to open up
later, impossible to put candor back.

Open questions for the team:

1. Which pilot team, and does their manager agree to consume both reports in
   parallel for three weeks?
2. Is there an existing Postgres instance we can use, or do we provision one?
3. Do we have a Jira service account, or does the collector use per-person
   tokens? Per-person is more accurate on permissions but far more setup.
4. Should the report go to a channel or stay an email? Channel is easier to
   build and easier to ignore.

## 12. Conventions

- No AI co-authorship trailers, no `Co-Authored-By` lines.
- Conventional commit prefixes, scoped to component: `feat(collector):`,
  `fix(slack):`.
- Type hints throughout; `mypy --strict` on `src/status/`.
- No secrets in the repo. `deploy/secrets.example.yaml` documents shape only.
- Skill prompts are versioned files in `skills/`, not strings in Python. Bump
  `PROMPT_VERSION` on every substantive change.

## 13. Attached references

- `SKILL.md` — the drafting skill, ready to iterate on
- `ledger_schema.sql` — full Postgres DDL with indexes, constraints, and the
  queries the schema is designed to make cheap
- `skills_client.py` — Skills upload, versioning, and invocation layer; lands at
  `src/status/skills/client.py`

Docs worth having open while implementing: the Skills API guide at
https://platform.claude.com/docs/en/build-with-claude/skills-guide and skill
authoring best practices linked from it.
