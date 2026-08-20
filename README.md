# Agentic Weekly Status Pipeline

Automated weekly status reporting: collect activity from Jira and GitHub, draft
per-person entries with Claude Skills, review in Slack, store in a Postgres
ledger, and synthesize a management report.

See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e ".[dev]"

export DATABASE_URL=postgresql+psycopg://localhost/weekly_status
alembic upgrade head

status --help
```

## CLI

```bash
status collect --person pilot --week 2026-08-14 --save-fixture
status draft   --fixture fixtures/pilot-2026-08-14.json --dry-run
status draft   --person pilot --week 2026-08-14
status send    --person pilot --week 2026-08-14
status slack run
status report  --week 2026-08-14 --dry-run
status skills list
status skills publish --skill drafter
```

Install Slack support: `pip install -e ".[slack]"`

## OpenShift (M3.5)

Deploy the Slack Socket Mode bot to OpenShift — no ingress required. See
[deploy/README.md](deploy/README.md) for build, secrets, and smoke-test steps.

## Project layout

```
skills/           Claude Agent Skill definitions
src/status/       Python pipeline
alembic/          Database migrations
deploy/           Dockerfile and OpenShift manifests
fixtures/         Saved collector payloads for offline testing
```
