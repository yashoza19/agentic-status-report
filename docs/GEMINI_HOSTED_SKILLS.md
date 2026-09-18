# Gemini Hosted Skills (Option A) — Design for Implementation

**Status:** Implemented (Option A)  
**Audience:** Implementing agent / engineer  
**Last updated:** 2026-09-18  
**Base branch:** `upstream/main` (opdev/agentic-status-report)  
**Suggested feature branch:** `feat/gemini-hosted-skills`

### Implementation decisions (recorded)

1. **One Managed Agent per skill** (drafter + synthesizer), not a single agent mounting both.
2. **REST + `google-auth` ADC** for Skill Registry / Agents / Interactions (SDK optional later; Agent update is REST-only in current docs). Optional extra: `pip install -e '.[gemini]'`.
3. **No `response_format` dependency** — always parse with `parse_json_model` and JSON follow-up on failure.
4. **Locations:** Skill Registry uses `GCP_LOCATION` (default `us-central1`); Agents + Interactions use `global` (`GCP_AGENTS_LOCATION` / `GCP_INTERACTIONS_LOCATION`).
5. **`base_agent` default:** `antigravity-preview-05-2026` via `GEMINI_BASE_AGENT`.
6. OpenAI hosted skills are **out of scope on this branch** (separate work); provider enum here is `anthropic | gemini`.
7. **Interactions invoke:** Agent calls require `stream=true`, `background=true`, `store=true`. After SSE, **GET the interaction** and concatenate `steps[].type == model_output` text fragments (ContentDelta / step fragments must not be joined with newlines). Prefer GET over raw SSE for the final answer.
8. **Agent id reservation quirk:** If create returns 409 but GET/PATCH return 404 (`being created` / soft-reserved), choose a new `*_AGENT_ID` (e.g. `ws-drafter-agent`). Do not silently fall back to Option B.

---

## 0. How to use this doc

Implement **Option A only**: Google Cloud **Skill Registry** + **Managed Agents** + **Interactions API**.  
Do **not** implement Option B (inline `SKILL.md` into a plain Gemini `generateContent` call) unless Option A is blocked by missing GCP APIs/access — in that case, document the blocker and stop.

After implementation, **test live Gemini skill publish + invoke** (drafter and synthesizer) against a real GCP project. Prefer fixtures for draft/report content when DB is unavailable; live skill calls are required.

---

## 1. Goal

Add `SKILL_PROVIDER=gemini` as a third hosted-skills backend alongside existing `anthropic` and `openai`, so:

1. `status skills publish --provider gemini --skill drafter|synthesizer|all` uploads/updates skills in **Gemini Skill Registry**.
2. Matching **Managed Agents** mount those skills from the registry.
3. `status draft` / `status report` invoke those agents via the **Interactions API** and parse JSON into existing Pydantic schemas (`DraftOutput`, `SynthesisOutput`).

**Non-goals**

- Replacing Anthropic/OpenAI providers
- Changing Slack review, collectors, ledger schema, or report markdown sanitizer behavior
- DOCX/email delivery
- Option B (prompt-inlined skills without Skill Registry)

---

## 2. Current architecture (do not break)

```
skills/{weekly-status-drafter,weekly-status-synthesizer}/SKILL.md
        │
        ▼
status skills publish --provider anthropic|openai
        │
        ▼
DRAFTER_SKILL_ID / SYNTHESIZER_SKILL_ID (+ optional VERSION)
        │
        ▼
status draft / status report
        │
        ▼
skill_invoke.invoke_skill_json(...)
   ├─ anthropic → SkillClient
   └─ openai    → OpenAISkillsClient (Responses + skill_reference)
        │
        ▼
parse_json_model → DraftOutput / SynthesisOutput
```

Key files today:

| Path | Role |
|------|------|
| `src/status/skills/skill_invoke.py` | Provider router |
| `src/status/skills/client.py` | Anthropic Skills |
| `src/status/skills/openai_skills.py` | OpenAI Skills + Responses |
| `src/status/skills/json_output.py` | Shared JSON parse / fence strip / retries |
| `src/status/cli.py` | `status skills list\|publish` |
| `src/status/config.py` | `SKILL_PROVIDER`, skill IDs, OpenAI/Anthropic settings |
| `skills/weekly-status-drafter/` | Drafter skill package |
| `skills/weekly-status-synthesizer/` | Synthesizer skill package |

Mirror the OpenAI client shape for Gemini; keep `invoke_skill_json` as the single runtime entry point.

---

## 3. Target architecture (Option A)

```
Local skills/ directory (SKILL.md zip)
        │
        ▼
Gemini Skill Registry
  projects/{PROJECT}/locations/{LOCATION}/skills/{skillId}
        │  (mount via base_environment.sources type=skill_registry)
        ▼
Managed Agents
  drafter agent id + synthesizer agent id
        │
        ▼
Interactions API
  POST .../interactions  { agent, input, stream?, store? }
        │
        ▼
Extract text → parse_json_model(schema) → existing pipeline
```

### Conceptual mapping

| This repo concept | Gemini concept |
|-------------------|----------------|
| Hosted skill package (`SKILL.md` zip) | Skill Registry **Skill** (+ immutable **skill revision**) |
| `DRAFTER_SKILL_ID` | Skill resource id / name (e.g. `weekly-status-drafter`) |
| OpenAI Responses `skill_reference` on one call | Managed Agent with skill mounted under `/.agent/skills/` |
| Runtime invoke | Interactions API with `agent=<AGENT_ID>` |
| API key auth | GCP Application Default Credentials / service account |

**Important:** Gemini does **not** accept a skill reference on a one-off chat call the way OpenAI Responses does. Runtime must target a **Managed Agent** that already mounts the Skill Registry skill. Publish therefore has two steps: (1) create/update skill, (2) ensure agent exists and mounts that skill.

---

## 4. Official docs (implementer must verify against current APIs)

APIs are evolving (`v1beta1`). Prefer SDK when stable; fall back to REST.

| Topic | Doc |
|-------|-----|
| Skill Registry overview | https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/skill-registry |
| Create/manage skills | https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/skill-registry/create-manage |
| Create/manage agents (skill mount) | https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/managed-agents/create-manage |
| Interact with agents | https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/managed-agents/interact-with-agents |
| Interactions API | https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/interactions-api |
| Vertex / Agent Platform Python SDK | https://docs.cloud.google.com/python/docs/reference/agentplatform/latest |
| Consumer Managed Agents quickstart (shape reference) | https://ai.google.dev/gemini-api/docs/managed-agents-quickstart |
| Example skill layout | https://github.com/google/skills |

### Expected Skill Registry create shape (REST)

```
POST https://{LOCATION}-aiplatform.googleapis.com/v1beta1/projects/{PROJECT}/locations/{LOCATION}/skills?skillId={SKILL_ID}
Authorization: Bearer {ACCESS_TOKEN}
Content-Type: application/json

{
  "displayName": "...",
  "description": "...",
  "zippedFilesystem": "<base64 zip containing SKILL.md (+ optional scripts/)>"
}
```

Create/update is often a **long-running operation** — poll until done.

SDK sketch (from Google docs; confirm package version):

```python
skill = client.skills.create(
    skill_id="weekly-status-drafter",
    display_name="weekly-status-drafter",
    description="...",
    config={"local_path": "./skills/weekly-status-drafter"},
)
```

### Expected agent skill mount

```json
{
  "id": "weekly-status-drafter-agent",
  "base_agent": "antigravity-preview-05-2026",
  "description": "Drafter for weekly status pipeline",
  "system_instruction": "Follow the mounted weekly-status-drafter skill. Return only the JSON object defined by the skill.",
  "base_environment": {
    "type": "remote",
    "sources": [
      {
        "type": "skill_registry",
        "source": "projects/PROJECT/locations/LOCATION/skills/weekly-status-drafter",
        "target": "/.agent/skills"
      }
    ],
    "network": {
      "allowlist": [{ "domain": "*" }]
    }
  }
}
```

Pin a revision when possible:

`projects/.../skills/{id}/skill_versions/{version}`

`base_agent` string and tool set may change — **read current create-agent docs** and pin a known-good base agent id in config (see env vars).

### Expected invoke (Interactions API)

```
POST https://aiplatform.googleapis.com/v1beta1/projects/{PROJECT}/locations/global/interactions
```

Body sketch:

```json
{
  "agent": "weekly-status-drafter-agent",
  "input": [
    {
      "type": "user_input",
      "content": [
        {
          "type": "text",
          "text": "<instruction>\n\nPAYLOAD:\n<json>"
        }
      ]
    }
  ],
  "stream": false,
  "store": false
}
```

Extract final assistant text (`output_text` or equivalent event fields). Reuse `status.skills.json_output.parse_json_model`. If first reply is fenced/non-JSON, follow the same retry pattern as OpenAI (`JSON_FOLLOW_UP`).

If Interactions supports `response_format` / JSON schema enforcement, prefer enabling it for `DraftOutput` / `SynthesisOutput` — but do not depend on it alone; always parse defensively.

---

## 5. Configuration

Extend `src/status/config.py` and `.env.example`:

| Env var | Required when `SKILL_PROVIDER=gemini` | Purpose |
|---------|----------------------------------------|---------|
| `SKILL_PROVIDER=gemini` | yes | Select Gemini branch |
| `GCP_PROJECT` / `GOOGLE_CLOUD_PROJECT` | yes | Project id |
| `GCP_LOCATION` | yes (default TBD; often `us-central1` or `global` per doc) | Skill Registry / agent location |
| `GCP_INTERACTIONS_LOCATION` | optional (default `global`) | Interactions API location if different |
| `GEMINI_BASE_AGENT` | yes (sensible default from docs) | Managed Agents `base_agent` id |
| `DRAFTER_SKILL_ID` | yes after first publish | Skill Registry skill id (stable string, e.g. `weekly-status-drafter`) |
| `SYNTHESIZER_SKILL_ID` | yes after first publish | Skill Registry skill id |
| `DRAFTER_SKILL_VERSION` | optional | Pin skill revision / `latest` |
| `SYNTHESIZER_SKILL_VERSION` | optional | Pin skill revision / `latest` |
| `DRAFTER_AGENT_ID` | yes after agent ensure | Managed Agent id for draft |
| `SYNTHESIZER_AGENT_ID` | yes after agent ensure | Managed Agent id for report |
| `GOOGLE_APPLICATION_CREDENTIALS` | as needed | Path to SA JSON (local/CI); Workload Identity in cluster |

**Auth:** Use Google ADC (`google.auth.default()`). Do not invent a Gemini API-key path for Option A Skill Registry / Managed Agents on Vertex Agent Platform.

**Reuse of skill id env vars:** Keep `DRAFTER_SKILL_ID` / `SYNTHESIZER_SKILL_ID` for Gemini skill registry ids (same names as Anthropic/OpenAI). Add **agent** ids as separate env vars because Gemini needs both.

Suggested defaults for agent ids if unset:

- `weekly-status-drafter-agent`
- `weekly-status-synthesizer-agent`

Suggested skill ids (immutable once created in a project):

- `weekly-status-drafter`
- `weekly-status-synthesizer`

---

## 6. Code changes (checklist)

### 6.1 New module: `src/status/skills/gemini_skills.py`

Implement `GeminiSkillsClient` (names flexible; behavior required):

- `upload(skill_dir: Path, skill_id: str) -> str` — create skill in registry
- `publish_version(skill_id: str, skill_dir: Path) -> str` — update / new revision
- `list_skills() -> list[dict]`
- `ensure_agent(*, agent_id: str, skill_id: str, skill_version: str, system_instruction: str) -> str` — create agent if missing; if present, PATCH mount/source to current skill revision
- `invoke_json(agent_id: str, payload: dict, instruction: str, schema: type[BaseModel], ...) -> BaseModel` — Interactions call + parse

Helpers:

- Zip skill dir for REST upload (reuse/adapt OpenAI zip helper if formats match; Gemini wants zip with `SKILL.md` at expected path — follow Google create-skill docs exactly)
- Poll LROs for create/update skill and create/update agent
- Extract text from interaction response / SSE if streaming is required

Raise `GeminiSkillError` (subclass `RuntimeError`) with actionable messages.

### 6.2 Router: `src/status/skills/skill_invoke.py`

- Extend `SkillProvider` to `"anthropic" | "openai" | "gemini"`
- `skill_provider()` accepts `gemini`
- `invoke_skill_json`: when gemini, require GCP project + agent id for the skill being invoked
  - Drafter path uses `DRAFTER_AGENT_ID` (settings)
  - Synthesizer path uses `SYNTHESIZER_AGENT_ID`
- Map skill_id → agent_id via settings (do not hardcode only one agent)

**Note:** Today `invoke_skill_json` receives `skill_id` from drafter/synthesizer modules. For Gemini, either:

1. Pass agent id as `skill_id` when provider is gemini (confusing), **or**
2. Prefer: look up agent id from settings based on which skill id was requested / add optional `agent_id` parameter.

Recommended: add optional `agent_id: str | None = None` to `invoke_skill_json`; drafter/synthesizer pass the correct agent id when provider is gemini; Anthropic/OpenAI ignore it.

### 6.3 Drafter / synthesizer entry points

- `src/status/skills/drafter.py` — allow gemini provider (API key checks today assume anthropic/openai; extend for GCP config presence)
- `src/status/skills/synthesizer.py` — same
- Keep existing instruction strings and schemas

### 6.4 CLI: `src/status/cli.py`

- `status skills list --provider gemini`
- `status skills publish --provider gemini --skill …`
  - Upload/update Skill Registry skill
  - Call `ensure_agent` for the matching agent
  - Print both `*_SKILL_ID` and `*_AGENT_ID` for `.env`
- Help text: `anthropic | openai | gemini`

Optional convenience (nice-to-have, not required):

- `status skills ensure-agents --provider gemini`

### 6.5 Dependencies

Add whatever is required, preferably as an optional extra, e.g.:

```toml
# pyproject.toml
[project.optional-dependencies]
gemini = [
  "google-cloud-aiplatform>=…",  # pin after verifying Skill Registry + Agents APIs
  "google-auth>=…",
]
```

Document: `pip install -e '.[gemini]'` (or whatever extra name you choose).  
If the Skill Registry / Managed Agents surface lives in a specific SDK submodule, pin the minimum version that includes `client.skills` / agents APIs (docs mention recent `google-cloud-aiplatform` releases).

### 6.6 Docs / examples

- Update `.env.example` with Gemini vars
- Short section in `docs/DEVELOPMENT.md` pointing at this doc
- Do not rewrite the main `DESIGN.md` narrative beyond a one-line “Gemini provider planned/added” if needed

### 6.7 Tests

Unit (no network):

- Zip packing / skill id validation
- Router selects gemini client when `SKILL_PROVIDER=gemini`
- Interaction response text extraction fixtures (sample JSON / SSE snippets)
- `parse_json_model` still works on Gemini-style fenced output if observed

Live (manual / gated):

- Mark with `@pytest.mark.integration` or a script under `scripts/`
- Skip unless `RUN_GEMINI_SKILLS_LIVE=1` and GCP creds present

---

## 7. Publish flow (must implement)

```bash
# From repo root, on feat/gemini-hosted-skills branched from upstream/main
export SKILL_PROVIDER=gemini
export GCP_PROJECT=...
export GCP_LOCATION=...
export GOOGLE_APPLICATION_CREDENTIALS=...   # if not using gcloud ADC user creds

status skills publish --skill drafter --provider gemini
# → creates/updates Skill Registry skill weekly-status-drafter
# → ensures Managed Agent weekly-status-drafter-agent mounts it
# → prints IDs to set in .env

status skills publish --skill synthesizer --provider gemini
# same for synthesizer
```

Idempotency:

- Re-publish updates skill content / creates new revision and refreshes agent mount to default or pinned revision
- Safe to run twice

---

## 8. Invoke flow (must implement)

Same user-facing CLI as today:

```bash
export SKILL_PROVIDER=gemini
export DRAFTER_SKILL_ID=weekly-status-drafter
export DRAFTER_AGENT_ID=weekly-status-drafter-agent
export SYNTHESIZER_SKILL_ID=weekly-status-synthesizer
export SYNTHESIZER_AGENT_ID=weekly-status-synthesizer-agent
# + GCP_* and credentials

# Drafter (fixture preferred for first live test)
status draft --fixture <path-to-fixture.json> --no-persist

# Or person/week if DB + collectors available
status draft --person yoza --week YYYY-MM-DD --no-persist

# Synthesizer (needs confirmed ledger rows OR dry-run)
status report --week YYYY-MM-DD --no-dry-run -o /tmp/status-gemini.md
```

Contract unchanged: skill returns JSON matching existing schemas; postprocess (evidence labels, sanitizer, etc.) stays in Python.

---

## 9. Testing plan (required for the implementing agent)

### 9.1 Branch hygiene

```bash
git fetch upstream
git checkout -b feat/gemini-hosted-skills upstream/main
# implement…
```

Do not base on personal fork `origin/main` if it lags `upstream/main`.

### 9.2 Automated

```bash
pytest -q tests/test_gemini_skills.py tests/test_skill_invoke.py  # or whatever you add
pytest -q   # full suite must still pass; anthropic/openai paths unchanged
```

### 9.3 Live Gemini skills test (acceptance)

Prerequisites: GCP project with Agent Platform / Skill Registry enabled; billing; IAM for the principal (Skill Admin / Agent User — use whatever roles the current docs require).

Steps:

1. `status skills publish --skill all --provider gemini`
2. Confirm skills listed: `status skills list --provider gemini`
3. Invoke drafter with a small fixture; assert JSON validates as `DraftOutput` (or CLI succeeds and prints entries)
4. Invoke synthesizer with a minimal `SynthesisInput` fixture **or** DB week with confirmed entries; assert markdown/JSON validates
5. Capture sample outputs under something like `draft-compare/draft-gemini.json` (optional, do not commit secrets)
6. Record in the PR / handoff note: project, location, skill ids, agent ids, any API quirks (LRO waits, base_agent pin, JSON fencing)

If live APIs are unavailable, the implementing agent must:

- Still land the code + unit tests
- Document exact blocker (API not enabled, IAM, region, SDK missing methods)
- Not silently fall back to Option B without calling that out

### 9.4 Regression

With `SKILL_PROVIDER=openai` or `anthropic` (if keys present), a single draft smoke test should still work — Gemini changes must be additive.

---

## 10. IAM / security notes

- Prefer least privilege SA for CI/OpenShift later; for local POC, user ADC via `gcloud auth application-default login` is OK
- Never commit service account JSON
- Skill packages may contain only instructions (current skills are docs + conventions); still treat registry as private to the project
- Managed Agent sandboxes mount skills with downscoped tokens (per Google sandbox docs) — do not embed long-lived secrets inside `SKILL.md`

---

## 11. Risks and open decisions

| Risk | Mitigation |
|------|------------|
| Agent Platform APIs still beta / renaming | Pin SDK version; isolate all Gemini calls in `gemini_skills.py` |
| `base_agent` id changes | Make `GEMINI_BASE_AGENT` configurable; document working value after first success |
| Skill create is async validation | Poll LRO; surface validation errors clearly |
| Interactions streaming vs non-streaming | Prefer non-stream for simpler parse; use stream only if required |
| Skill id immutable / reserved after delete | Choose stable kebab-case ids up front |
| Dual IDs (skill + agent) confuse operators | CLI prints both; `.env.example` comments both |

Open decision for implementer (record choice in PR):

1. One agent per skill (recommended) vs one agent mounting both skills  
2. Whether to force JSON `response_format` when available  
3. Exact Python package / import path once verified against installed SDK

---

## 12. Acceptance criteria

- [ ] Branch cut from `upstream/main`
- [ ] `SKILL_PROVIDER=gemini` routes draft + report through Gemini client
- [ ] `status skills publish --provider gemini` creates/updates Skill Registry skills **and** ensures Managed Agents mount them
- [ ] `status skills list --provider gemini` works
- [ ] Live invoke of drafter skill returns schema-valid JSON
- [ ] Live invoke of synthesizer skill returns schema-valid JSON / usable markdown
- [ ] Unit tests for client helpers + router; full pytest suite green
- [ ] `.env.example` and this design’s env table match what was implemented
- [ ] Anthropic/OpenAI paths unchanged when provider ≠ gemini
- [ ] PR description includes live test evidence or explicit blocker

---

## 13. Suggested implementation order

1. Config + `GeminiSkillError` + zip/LRO helpers  
2. Skill Registry create/update/list  
3. Managed Agent ensure (create + mount)  
4. Interactions invoke + JSON parse reuse  
5. Wire `skill_invoke` + drafter/synthesizer guards  
6. CLI publish/list  
7. Unit tests  
8. Live publish + invoke tests  
9. Docs / `.env.example`  
10. Open PR against `upstream/main`

---

## 14. Prompt for the implementing agent (copy/paste)

```text
Implement docs/GEMINI_HOSTED_SKILLS.md (Option A only) on a new branch
feat/gemini-hosted-skills cut from upstream/main.

Add Gemini Skill Registry + Managed Agents + Interactions API support as
SKILL_PROVIDER=gemini, parallel to anthropic/openai. Do not implement Option B
unless Option A is blocked — if blocked, document why.

Wire status skills publish/list and draft/report invoke. Reuse
parse_json_model and existing DraftOutput/SynthesisOutput schemas.

Add unit tests. Run live Gemini publish + invoke tests for drafter and
synthesizer. Update .env.example. Open a PR against upstream/main with
test evidence or blockers.
```

---

## 15. Reference: existing OpenAI pattern to mirror

See `src/status/skills/openai_skills.py`:

- Zip upload
- Version publish
- Invoke with skill attached
- JSON follow-up on parse failure

Gemini differs mainly in **agent mount + Interactions**, not in the pipeline’s JSON contract.
