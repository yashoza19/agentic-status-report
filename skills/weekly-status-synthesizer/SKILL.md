---
name: weekly-status-synthesizer
description: Synthesizes confirmed weekly status entries from a team ledger into a formatted management report organized by Partner Enablement, Certification/CI, and Mindshare. Use when generating the weekly team status report, formatting entries for management, or producing status-YYYY-MM-DD.md from structured ledger data. Preserves all formatting rules from the weekly_status format-status command including categorization, hyperlink style, and alphabetical sorting.
---

# Weekly status synthesizer

You produce the **management report** from confirmed ledger entries across the
team. This is the second stage of the pipeline — individual drafting already
happened. Your job is categorization, formatting, and rollup, not inventing
new content.

Every sentence in the report must trace to a confirmed `outcome` in the input
entries. Do not add claims, impact statements, or narrative that is not in the
source data.

## Input contract

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
  "participation": [{
    "person_id": "string",
    "display_name": "string",
    "status": "confirmed | expired | on_leave | sent | send_failed"
  }],
  "flags": [{
    "message": "string",
    "person_id": "string | null",
    "epic_key": "string | null"
  }]
}
```

## Report structure

Produce markdown matching this template:

```markdown
# Aug 14, 2026

## Partner Enablement

* **IBM** - Further discussions regarding [Spectrum Symphony Operator](https://issues.redhat.com/browse/EET-4853)...

## Certification / CI

* **Chart-Verifier** - Released [Chart Verifier 1.14.0](https://github.com/...)...

## Mindshare

* **Upstream Open Source Leadership** - Opened a PR to remove [unnecessary loops](https://github.com/...)...
```

Date format: `Aug 14, 2026` (month abbreviation, day, year).

## Categorization rules

Assign each entry to exactly one main section:

- **Partner Enablement** — work directly with partners/customers on their specific projects or deployments
- **Certification / CI** — work on certification tools, programs, and infrastructure (Preflight, Chart-Verifier, OCO, Helm Cert, etc.)
  - Even if the work involves helping partners, if it is work ON a certification tool itself, it belongs in Certification/CI
  - Example: Fixing a bug in Chart-Verifier → Certification/CI → Chart-Verifier, NOT Partner Enablement
- **Mindshare** — upstream contributions, conferences, blogs, and tutorials

Special routing:
- Anything regarding Red Hat Marketplace → **CI Pipeline** subsection
- Anything related to TSSC (Trusted Software Supply Chain) → **Red Hat Developer Hub** in Certification/CI

Use `epic_name` as the bold inline name when available. Fall back to `project` or a sensible subsection name.

**Exclude entirely:** entries with state `quiet` unless the team needs visibility on ongoing quiet epics (omit rather than pad).

**Do NOT include:** Learning, PTO / No Status, Missing Status sections.

## Rollup rules

- Organize by epic/project subsection, **not by person**. Managers track initiatives.
- Multiple entries for the same epic from different people → combine into one bullet using semicolons.
- Sort entries alphabetically within each main section.
- Non-responders (`participation.status = expired`) → add a note at the end of the relevant section or a brief "Team participation" note: "No update from {name} this week." Never silently omit.
- Every non-null `ask` from any entry must appear in a **Decisions needed** subsection (add after Mindshare if any asks exist). Render verbatim or near-verbatim.
- `flags` from input → render as plain observations, not risk characterizations.

## Formatting rules

- Each entry: `* **Name** - description` (inline bold, NOT `###` headers per entry)
- Multiple related bullets for the same partner/project → combine into one bullet with semicolons
- **After semicolons, always use lowercase**
- Never re-reference the partner/project name in the description after `* **Name** -`
- Don't put hyperlinks in parenthesis — attach to relevant words
- **CRITICAL: Never display raw Jira ticket IDs in visible text**
  - Bad: `EET-5174: Provided a workaround...`
  - Good: `Provided a workaround for [multi image provisioning issue](https://issues.redhat.com/browse/EET-5174)...`
  - Ticket IDs should ONLY appear in hyperlink URLs
- Jira links on noun phrases, not verbs: `investigated [hosted pipeline failure](url)` not `investigated hosted pipeline failure`
- GitHub links on "PR" or descriptive noun phrase
- Abbreviate "pull request" as "PR"; prefix numbers with `#` (e.g., `PR #104`)
- Capitalize: Helm, Operator
- Be concise; past tense for completed work
- Include context where present in source `outcome` text

## Evidence and hyperlinks

- Build Jira URLs: `https://issues.redhat.com/browse/{KEY}` for Red Hat Jira keys
- Use `evidence` array URLs directly for GitHub links
- Every hyperlink from source evidence must appear in the output
- Do not invent URLs not present in evidence

## Output

Return only this JSON. No markdown fences around the whole response, no preamble.

```json
{
  "week_ending": "YYYY-MM-DD",
  "markdown": "full markdown report as a string",
  "sections_used": ["Partner Enablement", "Certification / CI"],
  "entries_cited": ["person_id:epic_key", "person_id:epic_key"],
  "non_responders": ["display_name"],
  "asks": ["verbatim ask strings included in report"]
}
```

`entries_cited` should list each input entry referenced, using `{person_id}:{epic_key or 'unticketed'}`.

## Non-goals

- Do not rewrite outcomes beyond formatting and light clarity edits for management audience
- Do not add impact statements not in source data
- Do not compare people or characterize output as strong/weak
- Do not produce DOCX or email — only markdown
