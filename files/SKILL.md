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
  "person": "string, Jira account id or handle",
  "week_start": "YYYY-MM-DD",
  "week_end": "YYYY-MM-DD",
  "jira_issues": [
    {
      "key": "AIPLAT-231",
      "summary": "string",
      "issue_type": "Story | Bug | Task | Spike",
      "status": "string",
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

Tickets with no epic go into a single entry per project, with
`epic_key: null` and `needs_human: true` — the human usually knows which
initiative it belonged to and can say so in one word.

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

`quiet` epics get **one line and nothing else**. Do not manufacture narrative for
a week where nothing happened; a manager reading "continued to monitor and
support ongoing efforts" learns nothing and trusts the next line less.

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
- work in this week's PRs with no corresponding Jira ticket

Flags are observations for the human, not accusations. Write them plainly:
`AIPLAT-190 has had no activity for 3 weeks.` Not: `AIPLAT-190 appears to be at risk.`

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
      "outcome": "one sentence, under 30 words, past tense, no adjectives of impact",
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
