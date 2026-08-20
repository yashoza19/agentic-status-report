-- Weekly status ledger
-- Grain: one current row per (person, epic, week_ending) in status_entry.
-- Append-only: corrections insert a new revision; nothing is updated in place
-- except the is_current flag on the superseded row.

BEGIN;

CREATE TYPE entry_state AS ENUM (
  'shipped', 'progressing', 'slipped', 'blocked', 'quiet'
);

CREATE TYPE entry_source AS ENUM (
  'drafted',          -- AI draft accepted verbatim
  'drafted_edited',   -- AI draft, human changed the wording
  'human_written'     -- human wrote it from scratch
);

CREATE TYPE confidence_level AS ENUM ('high', 'medium', 'low');

CREATE TYPE participation_status AS ENUM (
  'sent', 'confirmed', 'expired', 'on_leave', 'send_failed'
);

-- ---------------------------------------------------------------------------

CREATE TABLE person (
  person_id         text PRIMARY KEY,
  display_name      text NOT NULL,
  slack_user_id     text UNIQUE,
  jira_account_id   text UNIQUE,
  github_login      text,
  manager_id        text REFERENCES person(person_id),
  active            boolean NOT NULL DEFAULT true
);

-- Epic dimension. current_name drifts over time; entries snapshot the name
-- they were reported under, and join here on the stable key.
CREATE TABLE epic (
  epic_key          text PRIMARY KEY,
  current_name      text NOT NULL,
  project           text NOT NULL,
  jira_status       text,
  last_seen_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------

CREATE TABLE status_entry (
  entry_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  week_ending         date NOT NULL,
  person_id           text NOT NULL REFERENCES person(person_id),

  -- epic_key is nullable: unticketed or un-epiced work is still reportable
  epic_key            text REFERENCES epic(epic_key),
  epic_name_snapshot  text,
  project             text NOT NULL,

  state               entry_state NOT NULL,
  outcome             text NOT NULL,
  blocker             text,
  ask                 text,

  -- Provenance. draft_outcome is what the skill produced; outcome is what the
  -- human confirmed. The delta is the quality signal for prompt tuning.
  draft_outcome       text,
  source              entry_source NOT NULL,
  confidence          confidence_level,
  needs_human         boolean NOT NULL DEFAULT false,
  prompt_version      text,

  -- Jira keys and PR urls supporting the outcome sentence.
  evidence            jsonb NOT NULL DEFAULT '[]'::jsonb,

  -- Escape hatch for skill-generated fields added later, so schema changes
  -- don't require a migration every time the prompt evolves.
  extra               jsonb NOT NULL DEFAULT '{}'::jsonb,

  revision            integer NOT NULL DEFAULT 1,
  supersedes_entry_id uuid REFERENCES status_entry(entry_id),
  is_current          boolean NOT NULL DEFAULT true,

  drafted_at          timestamptz,
  confirmed_at        timestamptz,
  confirmed_by        text REFERENCES person(person_id),
  created_at          timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT outcome_not_blank CHECK (length(btrim(outcome)) > 0),
  CONSTRAINT evidence_is_array CHECK (jsonb_typeof(evidence) = 'array'),
  CONSTRAINT week_ending_is_friday CHECK (extract(isodow from week_ending) = 5)
);

-- One live row per person/epic/week. Nullable epic_key needs coalesce so that
-- multiple un-epiced entries in the same week don't collide.
CREATE UNIQUE INDEX status_entry_current_uniq
  ON status_entry (person_id, week_ending, coalesce(epic_key, entry_id::text))
  WHERE is_current;

CREATE INDEX status_entry_week_idx    ON status_entry (week_ending) WHERE is_current;
CREATE INDEX status_entry_epic_idx    ON status_entry (epic_key, week_ending) WHERE is_current;
CREATE INDEX status_entry_review_idx  ON status_entry (week_ending) WHERE is_current AND needs_human;
CREATE INDEX status_entry_evidence_gin ON status_entry USING gin (evidence);

-- ---------------------------------------------------------------------------
-- Did the human respond at all? Distinguishes "quiet week" from "bot broke".

CREATE TABLE participation (
  person_id       text NOT NULL REFERENCES person(person_id),
  week_ending     date NOT NULL,
  status          participation_status NOT NULL,
  draft_sent_at   timestamptz,
  confirmed_at    timestamptz,
  reminder_count  integer NOT NULL DEFAULT 0,
  regenerated     boolean NOT NULL DEFAULT false,
  regenerate_reason text,
  note            text,
  PRIMARY KEY (person_id, week_ending)
);

-- ---------------------------------------------------------------------------
-- Which entries went into which report. This is the audit chain.

CREATE TABLE report_run (
  run_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  week_ending     date NOT NULL,
  generated_at    timestamptz NOT NULL DEFAULT now(),
  prompt_version  text NOT NULL,
  model           text NOT NULL,
  output_uri      text,
  delivered_at    timestamptz,
  superseded      boolean NOT NULL DEFAULT false
);

CREATE INDEX report_run_week_idx ON report_run (week_ending, generated_at DESC);

CREATE TABLE report_entry (
  run_id    uuid NOT NULL REFERENCES report_run(run_id) ON DELETE CASCADE,
  entry_id  uuid NOT NULL REFERENCES status_entry(entry_id),
  section   text,
  PRIMARY KEY (run_id, entry_id)
);

-- ---------------------------------------------------------------------------
-- Gap-detection output from the drafting skill. Kept separate from entries
-- because flags are observations about absence, not reported work.

CREATE TABLE flag (
  flag_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  week_ending   date NOT NULL,
  person_id     text REFERENCES person(person_id),
  epic_key      text REFERENCES epic(epic_key),
  flag_type     text NOT NULL,
  message       text NOT NULL,
  acknowledged  boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX flag_week_idx ON flag (week_ending) WHERE NOT acknowledged;

COMMIT;

-- ---------------------------------------------------------------------------
-- Queries the schema is designed to make cheap.

-- Epics with no reported movement in three weeks
-- SELECT e.epic_key, e.current_name, max(s.week_ending) AS last_movement
--   FROM epic e
--   LEFT JOIN status_entry s
--     ON s.epic_key = e.epic_key AND s.is_current AND s.state <> 'quiet'
--  GROUP BY e.epic_key, e.current_name
-- HAVING max(s.week_ending) < current_date - interval '21 days'
--     OR max(s.week_ending) IS NULL;

-- How often the human rewrites the draft (drafting-prompt quality signal)
-- SELECT week_ending,
--        count(*) FILTER (WHERE source = 'drafted')        AS accepted,
--        count(*) FILTER (WHERE source = 'drafted_edited') AS edited,
--        count(*) FILTER (WHERE source = 'human_written')  AS written
--   FROM status_entry WHERE is_current
--  GROUP BY week_ending ORDER BY week_ending DESC;

-- Open asks that have never appeared in a delivered report
-- SELECT s.week_ending, s.person_id, s.epic_key, s.ask
--   FROM status_entry s
--   LEFT JOIN report_entry re ON re.entry_id = s.entry_id
--  WHERE s.is_current AND s.ask IS NOT NULL AND re.entry_id IS NULL;

-- Quarter rollup for one epic, in reported order
-- SELECT week_ending, state, outcome
--   FROM status_entry
--  WHERE is_current AND epic_key = $1
--    AND week_ending BETWEEN $2 AND $3
--  ORDER BY week_ending;
