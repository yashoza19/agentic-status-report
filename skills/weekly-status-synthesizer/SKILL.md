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
    "evidence": ["EET-4853", "https://github.com/..."],
    "evidence_labels": {"EET-4853": "hosted pipeline failure"},
    "report_category": "Partner Enablement | Certification / CI | Mindshare",
    "report_name": "legacy-compatible management-facing initiative name"
  }],
  "participation": [{
    "person_id": "string",
    "display_name": "string",
    "status": "confirmed | expired | on_leave | sent | send_failed | collect_failed | draft_failed | nudge_failed"
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

`report_category` and `report_name` are authoritative metadata assigned before
synthesis. Put each entry under its supplied `report_category` and use its
supplied `report_name` as the bold bullet label. Do not reclassify an entry or
replace the label with a repository, epic, or person name.

The following definitions explain the taxonomy for writing and rollup; they are
not permission to override the supplied metadata.

Assign each entry to exactly one main section:

- **Partner Enablement** — work directly with partners/customers on their specific projects or deployments
  - Partner certification support (Oracle, IBM, ITRS, SAS, etc.) → Partner Enablement under the **partner name**, not Certification/CI
  - Example: "Helped Oracle publish DB Operator certification" → `* **Oracle** - ...` under Partner Enablement
- **Certification / CI** — work on certification tools, programs, and infrastructure (Preflight, Chart-Verifier, OCO, Helm Cert, etc.)
  - Even if the work involves helping partners, if it is work ON a certification tool itself, it belongs in Certification/CI
  - Example: OpenShift Cluster Management Bot epic, Chart-Verifier releases → Certification/CI
  - Example: Fixing a bug in Chart-Verifier → Certification/CI → Chart-Verifier, NOT Partner Enablement
- **Mindshare** — upstream contributions, conferences, blogs, and tutorials

Special routing:
- Anything regarding Red Hat Marketplace → **CI Pipeline** subsection
- Anything related to TSSC (Trusted Software Supply Chain) → **Red Hat Developer Hub** in Certification/CI

**Subcategory normalization** (match legacy `parse_and_format.py` behavior):

- Mindshare entries that are partner-facing updates → **Partner Enablement → OCP-V Partner Onboarding Strategy**
- Certification/CI: `olm` / Operator Lifecycle Manager, `oco` → Operator Certification Operator, `rukpak` → RukPak, `catalogd` → Catalogd, pipeline-alerts → CI Pipeline, MCP Cert, Vulnerability Scanner Cert / rhacs
- Mindshare: conference → Upcoming Conferences, blog/tutorial → Blogs/Tutorials, workshop → Workshops
- Partner Enablement: openshift virtualization / ocp-v → OCP-V Partner Onboarding Strategy

**Exclude entirely:** entries with state `quiet` unless the team needs visibility on ongoing quiet epics (omit rather than pad).

**Do NOT include:** Learning, PTO / No Status, Missing Status sections.

## Rollup rules

- Organize by epic/project subsection, **not by person**. Managers track initiatives.
- Multiple entries for the same epic from different people → combine into one bullet using semicolons.
- Sort entries alphabetically within each main section.
- Put participation notices in one `## Team participation` section after the
  three management sections. Use the status-specific behavior below; never
  describe a system failure as an employee failing to respond.
  - `confirmed`: no notice.
  - `expired`: "No update from {name} this week."
  - `sent`: no notice; the review window is still open.
  - `on_leave`: no notice; do not create PTO or leave reporting sections.
  - `send_failed`: "The draft status could not be delivered to {name}."
  - `collect_failed`: "Status evidence collection failed for {name}."
  - `draft_failed`: "Draft status generation failed for {name}."
  - `nudge_failed`: "The status reminder could not be delivered to {name}."
- Every non-null `ask` from any entry must appear in a **Decisions needed** subsection (add after Mindshare if any asks exist). Render verbatim or near-verbatim.
- **Never include gap-detection or data-quality commentary** — omit phrases like "no linked Jira tickets", "no linked commits or PRs", "backlog status in Jira despite PRs merging", or similar audit observations. Report only what work happened (`outcome`), blockers, and asks.
- Omit a backlog-only item when the outcome contains no current-week action,
  decision, comment, transition, or linked implementation work.

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
- GitHub links belong on a descriptive phrase that explains the work, never on
  a bare `PR #104` label.
- Do not enumerate implementation artifacts for management. Mention at most two
  visible evidence links in one bullet, even when the entry contains many URLs.
- The supplied `evidence` has already been reduced to the display-worthy subset.
  Never add an "additional evidence" clause, enumerate unused evidence, or emit
  raw commit hashes.
- Prefer a Jira initiative link over individual PR links when it supports the
  same statement. Otherwise select the one or two PRs that best represent the
  outcome.
- Preserve all unused URLs in the input evidence/audit trail; omission from the
  visible Markdown does not discard evidence.
- Capitalize: Helm, Operator
- Be concise; past tense for completed work
- Include context where present in source `outcome` text
- Lead with the concrete action and technical subject, not repository mechanics.
  Translate "merged five PRs" into what those changes did, using only details
  already present in the confirmed outcomes.
- Avoid vague standalone phrases such as "worked on", "tracked", or "advanced
  discussions". When the source contains the detail, name the specific problem,
  decision, component, result, or remaining state.
- Never use generic link text such as "implementation change". If the input does
  not provide a meaningful phrase, omit that link from visible Markdown.
- Never replace missing detail with ordinal placeholders such as "one discussion
  item", "another item", "the first ticket", or "related work". If confirmed
  outcomes do not say what completed or remains in progress, preserve no claim
  beyond the supported subject; do not manufacture a comparison from statuses.

## Evidence and hyperlinks

- Build Jira URLs: `https://issues.redhat.com/browse/{KEY}` for Red Hat Jira keys
- Use `evidence` array URLs directly for GitHub links
- Evidence is an audit set, not a display checklist. Show only the primary one
  or two links needed to substantiate the management-facing sentence.
- Do not invent URLs not present in evidence
- For Jira keys, use `evidence_labels[KEY]` as the link text when present (mid-sentence noun phrase)
- When `evidence_labels` is missing for a key, derive link text only from words already in `outcome` — do not invent titles
- **Never write `[text]` without a `(url)`** — invalid markdown links are rejected
- Do not add a `## Notes` section

## Output

Return only this JSON as plain text in a text block. Write the JSON directly — do not use the code execution tool. No markdown fences around the whole response, no preamble.

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
