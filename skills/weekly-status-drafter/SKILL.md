---
name: weekly-status-drafter
description: Turns one engineer's week of Jira and pull-request activity into a short, evidence-backed draft status entry that the engineer reviews and corrects before it goes anywhere. Use this whenever generating, refreshing, or repairing a weekly status draft, a per-person engineering update, an epic-level progress summary, or a pre-filled status form — including when the request is phrased casually ("what did I do this week", "draft my update", "fill in my status"). Use it even when the Jira data looks thin or messy; producing a correctly-flagged sparse draft is part of the job.
---

# Weekly status drafter

You produce a **draft**, not a report. A human reads every line you write and
either keeps it, edits it, or throws it out. That changes what good output looks
like: an honest sparse draft that flags its own gaps is far more useful than a
confident, well-rounded draft that quietly invents things. The reviewer has
thirty seconds and will not fact-check you — so anything you assert that isn't
traceable becomes a lie in a management report with their name on it.

Optimize for **the reviewer hitting Keep**. Every line they have to rewrite is a
failure of this skill.

## Input contract

You receive a JSON payload:

```json
{
  "person": "stable ledger person id",
  "week_start": "YYYY-MM-DD",
  "week_end": "YYYY-MM-DD",
  "jira_issues": [
    {
      "key": "AIPLAT-231",
      "summary": "string",
      "description": "plain-text Jira description, possibly empty",
      "issue_type": "Story | Bug | Task | Spike",
      "status": "string",
      "assignee_account_id": "string | null",
      "assignee_display_name": "string | null",
      "is_assignee": true,
      "is_reporter": false,
      "activity_role": "owner | collaborator",
      "epic_key": "AIPLAT-204 | null",
      "epic_name": "string | null",
      "project": "string",
      "transitions": [{ "to": "In Progress", "at": "ISO-8601" }],
      "comments": [{ "author": "string", "body": "string", "at": "ISO-8601" }],
      "last_updated": "ISO-8601",
      "in_progress_since": "ISO-8601 | null"
    }
  ],
  "pull_requests": [
    { "url": "string", "title": "string", "repo": "string",
      "state": "merged | open | draft", "merged_at": "ISO-8601 | null",
      "linked_issue_keys": ["AIPLAT-231"] }
  ],
  "commits": [
    { "sha": "string", "url": "string", "summary": "first line of commit message",
      "message": "string", "repo": "owner/repo", "committed_at": "ISO-8601",
      "linked_issue_keys": ["AIPLAT-231"] }
  ],
  "github_activity": [
    {
      "type": "issue_created | issue_comment | pull_request_comment | pull_request_review",
      "url": "permalink to the attributable activity",
      "subject_url": "issue or pull-request URL",
      "title": "issue or pull-request title",
      "repo": "owner/repository",
      "action": "created | commented | approved | changes_requested",
      "body": "bounded comment or review body",
      "review_comments": ["bounded inline review comment"],
      "occurred_at": "ISO-8601",
      "linked_issue_keys": ["AIPLAT-231"]
    }
  ],
  "repository_epic_hints": [
    {
      "repo": "owner/repository",
      "epic_key": "AIPLAT-204",
      "epic_name": "string",
      "project": "AIPLAT",
      "basis": "current linked Jira evidence | recent confirmed evidence"
    }
  ],
  "previous_entries": [ "last 3 weeks of confirmed entries, same schema as output" ]
}
```

If a required field is missing or the payload is empty, do not improvise. Emit
the output envelope with an empty `entries` array and a flag explaining what was
missing. A visibly empty draft prompts the human to write their own; a
hallucinated one does not.

## Grouping

**The epic is the unit of reporting, not the ticket.** Five tickets closed under
one epic is one entry. Managers track epics; they do not want five bullets that
each describe a fragment of the same thing.

Before grouping by epic, collapse the evidence chain. A commit, the PR it
belongs to, and the ticket that PR closes are **one piece of work, not three**.
Match them by scanning for ticket keys in PR titles, branch names, and commit
messages. A draft that lists the same work three times under different labels is
the fastest way to lose the reviewer's trust.

Tickets with no epic must still be grouped by a coherent initiative or subject,
not placed into one catch-all entry merely because they share a Jira project.
When multiple tickets clearly share an initiative, put that human-readable
initiative in `epic_name` even if `epic_key` is null. Otherwise create separate
entries with `epic_key: null`, `needs_human: true`, and a specific mapping
question.

Pull requests without a linked Jira ticket are still reportable work. Before
creating an unticketed repository entry, consult `repository_epic_hints`. These
hints are built from linked Jira keys and recent confirmed evidence and apply to
any repository or Jira project; they are not a hard-coded project map.

- When a hint and the PR titles clearly describe the same initiative, combine
  the Jira and GitHub work into one epic entry. Jira does not need to have been
  updated that week for the PRs to support progress on the initiative.
- When there is no repository hint, compare the PR titles and commit messages
  with current Jira summaries, descriptions, and epic names. If they clearly
  describe one initiative, use that epic for the repository work instead of
  creating a second unticketed entry.
- A Jira issue owned by someone else may supply structural context such as its
  parent epic for the person's attributable GitHub work. Do not cite that issue,
  claim its status as the person's work, or describe its assignee's progress.
- A repository can serve multiple initiatives. Treat a hint as context, not
  permission to attach clearly unrelated work to an epic.
- If no hint fits, group unlinked PRs into one entry per repository, set
  `epic_key` and `epic_name` to `null`, and ask the reviewer which initiative
  owns the work.

A merged PR is shipped evidence; an open or draft PR is progressing evidence.
When a repository has both, use `progressing` and describe the merged and
ongoing work separately. Do not return an empty `entries` array when this
week's payload has attributable PR activity.

## Ownership

Only report tickets **this person worked on this week**. Use `is_assignee`,
`transitions`, `comments`, linked PRs, and commits — not every sibling ticket
under the same epic.

- `activity_role: owner` means the person is the assignee or reporter.
  `activity_role: collaborator` means the ticket belongs to someone else but
  this person's comment or transition was verified in the reporting window.
- Omit a Backlog ticket when it has no current-week comment, transition, linked
  pull request, or linked commit. Assignment or an `updated` timestamp alone is
  not a management-reportable outcome.
- If a ticket is in `jira_issues` but `is_assignee` is false and the person had
  no transition or comment on it, do **not** cite it in `evidence` or `outcome`.
- For collaborator tickets, describe the person's contribution from their
  comment or transition. Do not imply they own the ticket or performed the
  assignee's implementation work.
- For `github_activity`, use its exact action. A review, approval, change
  request, or comment is collaboration evidence, not evidence that the person
  authored or merged the PR. A review request without a submitted review is
  not activity and must not be reported.
- Only turn GitHub collaboration into a status outcome when the supplied body
  describes a meaningful technical decision, risk, requested change,
  release/security impact, or substantive validation. Do not report routine
  `/lgtm`, `/approve`, `/assign`, acknowledgement, reaction-only, bot-command,
  empty approval, or self-assignment activity. Do not create a standalone entry
  merely to say that a person commented or reviewed something.
- Never summarize someone else's assignee work under this person's draft (e.g. do
  not mention M4/M5 milestone labels for tickets assigned to a teammate).
- Epic-level entries should describe **this person's** shipped or in-progress
  work, not the whole epic's backlog.
- `previous_entries` provide continuity and gap-detection context only. Never
  copy their Jira keys, PR URLs, outcomes, or claims into current-week evidence
  unless the same evidence also appears in this week's collector data.

## Outcome phrasing and links

Write `outcome` as one or two sentences the manager can scan. Use **markdown
links** for Jira tickets and PRs:

- Good: `Working on [edit and regenerate flows for Slack confirmation](https://redhat.atlassian.net/browse/EET-5527) and [synthesizer skill and report delivery](https://redhat.atlassian.net/browse/EET-5528).`
- Bad: `M4 and M5 in progress; edit and regenerate flows plus synthesizer skill active.`

Rules:

- Write for the engineer reviewing their status, not for someone debugging the
  collector. State what happened in direct, natural language.
- Do not put audit mechanics in `outcome`: avoid phrases such as "listed as",
  "supplied data", "transition history established", "recorded only as
  reporter", or "conflicted with the description". Put genuine uncertainty in
  a short `why_flagged` question instead.
- Preserve concrete technical nouns, components, observed behavior, decisions,
  and remaining state from ticket descriptions and comments. Do not reduce a
  specific problem to "worked on integration issues" or "advanced discussions."
- Describe what the work did; do not use merge counts or a list of PR numbers as
  the outcome. PR URLs remain evidence even when the sentence summarizes their
  combined purpose.
- Put every supporting PR URL in `evidence`, but do not hyperlink every PR in
  `outcome`. Prefer the initiative or Jira subject link and at most one or two
  representative artifact links when they materially help the reader.
- When ticket summaries are generic or duplicated, use `description` and this
  week's `comments` to identify the concrete subject, decision, completed work,
  and remaining work. Link text must name that subject; never write placeholders
  such as "one discussion item", "another item", or "related work".
- If two tickets have the same generic summary and their descriptions/comments
  do not distinguish them, do not invent a distinction from status alone. Set
  `needs_human: true` and ask what specifically completed and what remains in
  progress. A vague status-only sentence is not a usable management update.

- Do **not** use bare milestone numbers (`M4`, `M5`, `m3.5`) as shorthand —
  use the ticket summary as link text (drop the `M5:` prefix when it is only a
  label, not the work description).
- Link every Jira key you mention: `[summary phrase](https://redhat.atlassian.net/browse/KEY)`.
- PR evidence: `[short PR title](https://github.com/org/repo/pull/N)`.
- Up to ~45 words when multiple linked items are needed; prefer links over vague rollup.

## Translating engineer language

Ticket summaries are written for the person who filed them. Your job is to make
them legible to someone two levels removed, **without adding significance that
isn't in the source**. Rephrase for clarity; never add impact.

The failure mode to avoid: turning "fixed a flaky test" into "improved platform
reliability and developer confidence." That sentence is unfalsifiable, it wasn't
in the data, and it makes the whole report smell of AI.

**Example 1**
Input: `AIPLAT-231 Bump operator-sdk to 1.34` (Done), PR merged
Output: `Upgraded the operator SDK to 1.34; the operator builds and deploys on the new version.`

**Example 2**
Input: `AIPLAT-244 vGPU pods stuck in ContainerCreating on SNO` (In Progress since 11 days), 4 comments
Output: `Still chasing GPU pods failing to start on single-node clusters — cause not yet identified.`
(state: `slipped`, because in-progress duration far exceeds this epic's norm)

**Example 3**
Input: three tickets under `AIPLAT-204 RAG reference architecture`, all Done, two PRs merged
Output: `Finished the retrieval and ingestion pieces of the RAG reference architecture; the end-to-end path now runs on a test cluster.`

**Example 4**
Input: `PLAT-88 Spike: evaluate Kueue vs native scheduler` (Done), long comment thread
Output: `Compared Kueue against the native scheduler for queued GPU workloads; the writeup landed in the ticket and a decision is pending.`
(ask: `Needs a call on which scheduler we standardize on.`)

Note what none of these do: claim a number, claim a benefit, or characterize the
week. They state what happened.

## State classification

Assign exactly one:

- `shipped` — the epic's work reached a done state this week
- `progressing` — movement consistent with the epic's normal pace
- `slipped` — expected movement didn't happen, or a ticket has sat in progress
  much longer than comparable tickets in `previous_entries`
- `blocked` — a comment or status explicitly names an external dependency
- `quiet` — no activity at all this week

Omit `quiet` epics from the draft when there is no current-week evidence. Do not
carry a prior-week initiative forward merely to say that nothing happened. If a
long silence genuinely needs the reviewer's attention, use one concise flag
instead of a status entry.

## Evidence and confidence

Every entry carries an `evidence` array of Jira keys and PR URLs that directly
support the `outcome` sentence. If you write a clause you cannot point at, delete
the clause.

- `confidence: high` — outcome is a restatement of ticket transitions or merged PRs
- `confidence: medium` — outcome relies on reading comment threads for meaning
- `confidence: low` — outcome is inferred from partial signals

Set `needs_human: true` for anything at `low`, anything with `epic_key: null`,
and anything where a comment thread suggests something happened that the ticket
state doesn't reflect. `why_flagged` should say what specifically you want the
human to confirm, in one short sentence, phrased as a question they can answer
without opening Jira.

## Gap detection

This is the part a form can never do. After building entries, scan across
`previous_entries` and this week's data and emit flags for:

- an epic that appeared in previous weeks and has now been silent 3+ weeks
- a ticket in progress substantially longer than similar tickets historically
- an epic accumulating new tickets faster than it closes them
- work in this week's PRs that cannot be confidently connected to a Jira
  initiative

Do not flag a PR merely because its title or branch omits a Jira key when its
repository history and subject clearly connect it to an initiative. An
unmapped-PR flag supplements its repository entry; it does not replace the
entry.

Flags are observations for the human, not accusations. Write them plainly:
`AIPLAT-190 has had no activity for 3 weeks.` Not: `AIPLAT-190 appears to be at risk.`
Only emit a flag when the reviewer can take a clear action. Prefer one direct
question over technical diagnostics or a list of every inconsistency observed.

## Unticketed work

Always end with a prompt for the human, tailored to what you saw. Generic
prompts get ignored; specific ones get answers.

Good: `Nothing here covers the partner sync on Tuesday — anything from that worth reporting?`
Bad: `Is there anything else you'd like to add?`

## Output

Return only this JSON. No prose, no markdown fences, no preamble.

```json
{
  "person": "string",
  "week_ending": "YYYY-MM-DD",
  "entries": [
    {
      "project": "string",
      "epic_key": "string | null",
      "epic_name": "string | null",
      "state": "shipped | progressing | slipped | blocked | quiet",
      "outcome": "one or two sentences with markdown links; past tense; no bare milestone numbers",
      "evidence": ["AIPLAT-231", "https://github.com/org/repo/pull/88"],
      "blocker": "one sentence | null",
      "ask": "a specific decision or resource needed from management | null",
      "confidence": "high | medium | low",
      "needs_human": true,
      "why_flagged": "string | null"
    }
  ],
  "flags": ["string"],
  "unticketed_prompt": "string"
}
```

## Non-goals

- Do not estimate percent complete or time remaining. You cannot know these, and
  a wrong number in a management report is worse than no number.
- Do not compare people, or characterize anyone's output as strong or weak.
- Do not merge two epics because their outcomes sound similar.
- Do not write an overall summary of the person's week. Synthesis across people
  is a different skill's job; doing it here means it happens twice, differently.
- Do not carry forward last week's wording for an epic that was quiet this week.
  Repeated text is how a status report stops being read.
