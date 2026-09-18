# Development guide

End-to-end setup for working on the weekly status pipeline locally: Postgres,
environment, the `status` CLI, and the collect → draft → send loop.

For architecture and milestones, see [DESIGN.md](DESIGN.md). For OpenShift
production deploy, see [deploy/README.md](../deploy/README.md).

## Prerequisites

| Tool | Version | Used for |
|------|---------|----------|
| Python | 3.11+ | CLI and pipeline |
| Postgres | 14+ | Ledger (`status_entry`, `person`, …) |
| `psql` | any | Seeding person rows, ad-hoc queries |
| `oc` + cluster access | optional | OpenShift Postgres (PGO) and port-forward |
| Slack app | optional | `status send` and `status slack run` |
| Anthropic API key | optional | `status draft` (hosted drafter skill) |
| Jira / GitHub tokens | optional | `status collect` live data |

`--week` must be a **Friday** (`YYYY-MM-DD`), matching `week_ending` in the ledger.

---

## 1. Install the `status` CLI

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -U pip
pip install -e ".[dev,slack]"
```

Confirm the console script is on your path:

```bash
status --help
```

If `status` is not found, ensure the venv is activated or run
`python -m status.cli --help`.

Run unit tests:

```bash
PYTHONPATH=src pytest
```

---

## 2. Environment (`.env`)

```bash
cp .env.example .env
```

Edit `.env` with your values. The app loads it automatically via
`pydantic-settings` (see `src/status/config.py`).

### Minimum for database-only work

```bash
DATABASE_URL=postgresql+psycopg://localhost/weekly_status
```

### Collect (Jira + GitHub)

```bash
JIRA_BASE_URL=https://redhat.atlassian.net
JIRA_EMAIL=you@redhat.com
JIRA_API_TOKEN=                    # Atlassian API token
JIRA_PROJECTS=EET                  # comma-separated project keys

GITHUB_TOKEN=                      # classic or fine-grained PAT
GITHUB_LOGIN=your-github-login
```

Jira collection uses `JIRA_EMAIL` for assignee/reporter JQL. Override per run with
`status collect --jira-email` when needed.

### Draft (Claude hosted skill)

```bash
ANTHROPIC_API_KEY=
CLAUDE_MODEL=claude-sonnet-5
DRAFTER_SKILL_ID=                  # after `status skills publish --skill drafter`
DRAFTER_SKILL_VERSION=latest
```

Publish or refresh the skill after editing `skills/weekly-status-drafter/`:

```bash
status skills publish --skill drafter
# copy printed skill id into .env if first time
```

### Draft / report with Gemini hosted skills (optional)

See [GEMINI_HOSTED_SKILLS.md](GEMINI_HOSTED_SKILLS.md) for Option A (Skill Registry +
Managed Agents + Interactions API).

```bash
pip install -e '.[gemini]'
export SKILL_PROVIDER=gemini
export GCP_PROJECT=your-project
export GCP_LOCATION=us-central1
# ADC: gcloud auth application-default login
status skills publish --skill all --provider gemini
# set DRAFTER_SKILL_ID / SYNTHESIZER_SKILL_ID and *_AGENT_ID from the CLI output
```

### Send (Slack DM)

```bash
SLACK_BOT_TOKEN=xoxb-...           # Bot User OAuth Token
SLACK_APP_TOKEN=xapp-...           # only for `status slack run` (Socket Mode)
```

Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) with
Socket Mode, bot scopes `chat:write`, `im:write`, `im:history`, `users:read`,
`commands`, and slash command `/weekly-status`.

---

## 3. Postgres setup

The ledger stores draft entries, flags, and participation. Pick **local Postgres**
for everyday development, or **OpenShift PGO** if you share the pilot cluster DB.

### Option A — Local Postgres (recommended for dev)

**Docker:**

```bash
docker run --name weekly-status-pg \
  -e POSTGRES_DB=weekly_status \
  -e POSTGRES_PASSWORD=postgres \
  -p 5432:5432 \
  -d postgres:16
```

```bash
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/weekly_status
```

**macOS (Homebrew):**

```bash
brew install postgresql@16
brew services start postgresql@16
createdb weekly_status
export DATABASE_URL=postgresql+psycopg://localhost/weekly_status
```

Add `DATABASE_URL` to `.env` so you do not need to export it each session.

### Option B — OpenShift (Crunchy Postgres Operator)

Use the cluster Postgres when you need parity with the pilot environment.

1. Install PGO and apply the cluster — full steps in
   [deploy/postgres/README.md](../deploy/postgres/README.md).
2. Run migrations on-cluster (`deploy/postgres/migrate-job.yaml`) or locally
   after port-forward (below).
3. Build `DATABASE_URL` from the PGO user secret
   `weekly-status-db-pguser-weeklystatus`.

**Port-forward** (keep this terminal open while using the local CLI):

```bash
export KUBECONFIG=/path/to/your/kubeconfig
oc port-forward svc/weekly-status-db-primary -n weekly-status 5432:5432
```

Decode credentials and set `DATABASE_URL` in `.env`:

```bash
NS=weekly-status
SECRET=weekly-status-db-pguser-weeklystatus

USER=$(oc get secret $SECRET -n $NS -o jsonpath='{.data.user}' | base64 -d)
PASS=$(oc get secret $SECRET -n $NS -o jsonpath='{.data.password}' | base64 -d)
DB=$(oc get secret $SECRET -n $NS -o jsonpath='{.data.dbname}' | base64 -d)
# URL-encode password if it contains special characters
echo "postgresql+psycopg://${USER}:${PASS}@localhost:5432/${DB}"
```

`status draft` only writes to Postgres at the end of a long Anthropic call. If the
tunnel drops during the skill run, persist fails — keep port-forward running or
run draft on-cluster (`deploy/job-draft.yaml`).

---

## 4. Apply schema migrations

With `DATABASE_URL` set (in `.env` or the shell):

```bash
alembic upgrade head
```

Verify:

```bash
psql "${DATABASE_URL/postgresql+psycopg/postgresql}" -c '\dt'
```

You should see `person`, `status_entry`, `flag`, `participation`, etc.

---

## 5. Add users to Postgres

Each engineer needs a `person` row before `status send` can DM them. The
collector can run without a row (using CLI overrides), but Slack delivery
requires `slack_user_id`.

```bash
psql "${DATABASE_URL/postgresql+psycopg/postgresql}" <<'SQL'
INSERT INTO person (person_id, display_name, slack_user_id, github_login)
VALUES (
  'yoza',
  'Yash Oza',
  'U01234567',              -- Slack member ID (Profile → ⋮ → Copy member ID)
  'yoza'                    -- GitHub login
)
ON CONFLICT (person_id) DO UPDATE SET
  display_name   = EXCLUDED.display_name,
  slack_user_id  = EXCLUDED.slack_user_id,
  github_login   = EXCLUDED.github_login;
SQL
```

| Column | Where to find it |
|--------|------------------|
| `person_id` | Short handle used in CLI (`-p yoza`) |
| `slack_user_id` | Slack profile → copy member ID (`U…` / `W…`) |
| `github_login` | GitHub username for commit/PR search |

Set `JIRA_EMAIL` in `.env` (or pass `--jira-email` on collect) for Jira activity.

`status draft` will auto-create a minimal `person` row on first persist if one
does not exist, but **without** `slack_user_id` — `status send` will fail until
you update the row.

---

## 6. Development workflow

Replace `yoza` and the week with your person id and a recent Friday.

### Step 1 — Collect activity

Live Jira + GitHub (needs tokens in `.env`):

```bash
status collect --person yoza --week 2026-08-21 \
  --save-fixture fixtures/yoza-2026-08-21.json
```

Inspect the JSON payload. Collection errors (missing token, Jira permission)
appear in `collection_errors` on the payload and as `flag` rows after draft.

Offline / repeatable test using a saved fixture:

```bash
status collect --person yoza --week 2026-08-21 --dry-run   # no API calls
```

### Step 2 — Draft status entries

Dry-run (no Anthropic call):

```bash
status draft --fixture fixtures/yoza-2026-08-21.json --dry-run
```

Persist to Postgres (calls drafter skill, ~1–2 min):

```bash
status draft --person yoza --week 2026-08-21
# or
status draft --fixture fixtures/yoza-2026-08-21.json
```

Check output:

- `entries` — per-epic draft rows with linked outcomes
- `persisted_entry_ids` — non-empty when rows were written
- `flags` — gap notes for human review

Preview without writing:

```bash
status draft --fixture fixtures/yoza-2026-08-21.json --no-persist
```

### Step 3 — Send Slack review DM

Requires `SLACK_BOT_TOKEN` and `slack_user_id` on the person row:

```bash
status send --person yoza --week 2026-08-21
```

The message lists draft entries (with Jira links), then review flags, then
**Looks right** / **Edit** / **Regenerate** buttons.

Dry-run:

```bash
status send --person yoza --week 2026-08-21 --dry-run
```

### Step 4 — Interactive Slack bot (optional)

For button handlers and `/weekly-status`:

```bash
status slack run
```

Requires `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`. In Slack, use `/weekly-status`
or confirm via the draft DM buttons.

### Step 5 — Management report (optional)

```bash
status report --week 2026-08-21 --dry-run
```

Needs confirmed ledger entries and synthesizer skill configuration for a live run.

---

## 7. Common commands cheat sheet

```bash
# Setup
pip install -e ".[dev,slack]"
cp .env.example .env
alembic upgrade head

# Skills
status skills list
status skills publish --skill drafter
status skills publish --skill synthesizer

# Pipeline
status collect -p <person> -w <friday> --save-fixture fixtures/<person>.json
status draft   -p <person> -w <friday>
status draft   -f fixtures/<person>.json
status send    -p <person> -w <friday>

# Bot
status slack run
```

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|----------------|-----|
| `status: command not found` | venv not activated | `source .venv/bin/activate` |
| `ModuleNotFoundError: status` | package not installed | `pip install -e ".[dev]"` |
| `connection refused` on draft | Postgres down or port-forward stopped | Start local Postgres or re-run `oc port-forward` |
| `persisted_entry_ids` empty / persist error | DB URL wrong or unique constraint | Check `DATABASE_URL`; re-run `status draft` |
| `unknown person` on send | no `person` row | Insert into `person` (§5) |
| `has no slack_user_id` | person row incomplete | Update `slack_user_id` in Postgres |
| Collect returns empty Jira | wrong `JIRA_EMAIL` or project scope | Verify email and `JIRA_PROJECTS` |
| Draft flags only, no entries | skill parse failure or empty payload | Re-run draft; check Anthropic key and `DRAFTER_SKILL_ID` |
| Slack send succeeds but no DM | bot not in workspace or wrong user id | Reinstall app; verify `slack_user_id` |

---

## 9. Related docs

- [DESIGN.md](DESIGN.md) — architecture and data model
- [deploy/postgres/README.md](../deploy/postgres/README.md) — PGO cluster, migrate job, on-cluster draft
- [deploy/README.md](../deploy/README.md) — OpenShift bot deployment
