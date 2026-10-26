"""M3 schema: score cycles, targets and attempts, drafts, critic notes, results and score instances, batches,
user actions, feedback, golden cases and their eval results, and the current-score views.

Revision ID: 0008_m3_scoring
Revises: 0007_m2_draft_text_edits

Source: docs/superpowers/specs/2026-10-16-job-hunter-design.md (revision 3.4), staged by
docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md (Task 1). The DDL below is copied verbatim from the
spec as static SQL, one `op.execute` per statement, in dependency order (A1 §0). Every foreign key to a table
that comes later is added afterwards with ALTER TABLE ... ADD FOREIGN KEY, at the place the spec has it:

  guard  an `active_config` row with a NULL rubric or prompt-schema pointer stops the migration with an error
         that names the remedy, M2's `migrate` (plan Deviation 5; without it such an install meets the closing
         SET NOT NULL as a bare constraint error)
  §7.6   score_cycles (+ score_cycles_one_running)
  §7.11  bulk_operations (+ bulk_operations_one_active), score_targets (its seven indexes),
         admission_lane_credits, score_attempts; then the foreign keys M2 staged on them (M2 Deviation 1):
           llm_calls.attempt_id             -> score_attempts(id)
           llm_calls.target_id              -> score_targets(id)
           llm_spend_ledger.bulk_operation_id -> bulk_operations(id) ON DELETE SET NULL
         (`llm_calls` and `llm_spend_ledger` are altered, never recreated, so M2 Deviation 30's two CHECKs
         stay, A1 issue 36); score_drafts, critic_notes, llm_results and the three deferrable foreign keys
         between targets, results and drafts, result_evidence, det_results (its four indexes)
  §7.12  batch_submissions (+ batch_one_in_flight) and score_targets.batch_submission_id; batch_items and its
         two deferrable foreign keys, the second closing llm_calls.batch_item_id -> batch_items(id)
  §7.13  user_job_actions, score_feedback, golden_cases, the foreign key llm_calls.golden_case_id ->
         golden_cases(id) ON DELETE CASCADE that closes M2's last staged column, eval_case_results
  §7.18  the views current_scores and display_scores, from `db/views.sql` read at migration time, so the
         migration and a later CREATE OR REPLACE share one source
  §7.16  one assert_transition trigger per M3 state machine: score_targets(state), score_attempts(outcome),
         score_cycles(status), bulk_operations(state), batch_submissions(state), batch_items(result_type);
         `migrate` writes their edges to transition_matrix from the code registry (Task 2)
  last   active_config.rubric_version_id and prompt_schema_version_id SET NOT NULL (M0 Deviation 1, M2 plan
         Deviation 32): the seed of M2's `migrate` sets both pointers, so the ALTER validates on every
         install that ran it

These are the ONLY differences from the spec text. Each one carries a SQL comment naming its owner:

  1. batch_submissions.reserved_usd and followup_held_usd are numeric(12,6), the precision of the ledger
     entries they sum (R7, plan Deviation 10).
  2. Three CHECKs beyond the DDL (plan Deviation 2): score_targets CHECK (transient_failures >= 0) (A1 issue
     4); bulk_operations CHECK (spent_usd >= 0), and no CHECK of spent_usd <= budget_usd, because a billed
     settlement above its reservation at a full allocation is counted in full (L1164) and may pass the
     budget by the violation, which llm.reservation_bound_violated and the budget_counters audit report (A1
     issue 29, Codex r1 #28); eval_case_results CHECK (repetition BETWEEN 1 AND 3) (A1 issue 12). The
     outcome of an eval case keeps the spec's five values: an interrupted eval run is failed and re-run,
     never resumed (plan Deviation 29).
  3. Columns beyond the DDL:
     - llm_spend_ledger.admitted_buffer_usd numeric(10,2), nullable (NULL on M2's rows): the overrun buffer in
       force at admission, so §7.17 #6's row inequality replays after a buffer edit (M2 Carry-forward, plan
       Deviation 2);
     - jobs.synthetic_eval and profile_versions.synthetic_eval, boolean NOT NULL DEFAULT false, the markers of
       the adversarial golden-case rows (R2, Task 19, plan Deviation 21), with the partial index
       jobs_synthetic_eval; current_scores leaves those jobs out (db/views.sql);
     - llm_results.critic_injection, the last valid review's sanitised injection assessment, or 'unavailable'
       when the critic ran and no structurally valid review exists (L3557-3558), with
       CHECK ((critic_injection IS NULL) = (critic_outcome = 'not_run')): the deterministic recomputation of
       the confidence needs it (L4534-4537; Codex r1 #11, r2 B17, plan Deviation 2);
     - batch_submissions.operation_id (-> operations ON DELETE SET NULL, with the index
       batch_submissions_operation) and operation_claim_id: the batch_submit operation and the execution claim
       that prepared the submission, so an execution lost at preparing or submit_started is recognised by its
       claim and recovered, never by a timeout (Codex r1 #23, r2 B12, plan Deviation 27);
       batch_submissions.follow_on_at, the once-only marker of the follow-on submission requested at this
       submission's end (Codex r2 B15);
     - score_attempts.lane, nullable: the lane of the admission that made the attempt, written by the claim
       and by the batch preparation, because score_targets.lane is overwritten by every later claim (Codex
       r3 B15, r4 B2, plan Deviation 2). Its CHECK and score_targets.lane's are written from one SQL
       literal, LANE_VALUES_SQL below, whose member list `admission.LANE_VALUES` pins (Task 12);
     - prompt_schema_versions.parser_family text NOT NULL DEFAULT 'generator_v1', the explicit parser family
       of the version's generator schema, written by the seed (Task 4, round-4 ruling (b), plan Deviation
       40); its CHECK list is pinned by `schemas.PARSER_FAMILIES` (Task 4);
     - batch_items.hold_usd numeric(12,6) NOT NULL DEFAULT 0 CHECK (hold_usd >= 0) and hold_released_at: the
       item's follow-up hold and its once-only release (Codex r1 #27, plan Deviation 27).
  4. Indexes beyond the DDL (plan Deviation 2, M1's note 9 precedent): one on each foreign key column that a
     retention or purge delete reaches through its referencing side (the block before the views).

The migration imports no application code and inserts no row (§19.3 step 2). Migrations are expand/contract:
there is no downgrade.
"""

# ruff: noqa: E501, RUF001  (the DDL is copied from the spec; its long lines and its dashes and minus signs stay as they are)

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from alembic import op

revision: str = "0008_m3_scoring"
down_revision: str | Sequence[str] | None = "0007_m2_draft_text_edits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The lanes of score_targets.lane (L2219-2222, in the spec's order) and of score_attempts.lane: one SQL
# literal for both CHECKs. `admission.LANE_VALUES` (Task 11) holds the same members and Task 12's
# `test_lane_values_pin_both_checks` keeps the two in step; a migration never imports application code.
LANE_VALUES_SQL: Final = (
    "('similarity','oldest','exploration','always_include',\n"
    "                                               'score_now','batch','batch_followup')"
)
# `db/views.sql`, next to the migrations package (versions -> migrations -> db).
VIEWS_SQL: Final = Path(__file__).resolve().parents[2] / "views.sql"


def view_statements() -> list[str]:
    """The statements of `db/views.sql`, in file order: each starts with CREATE at the beginning of a line,
    and a part before the first one holds comments only."""
    text = VIEWS_SQL.read_text(encoding="utf-8")
    parts = text.split("\nCREATE ")
    head, statements = parts[0], [f"CREATE {part.strip()}" for part in parts[1:]]
    if any(line.strip() and not line.lstrip().startswith("--") for line in head.splitlines()):
        raise ValueError(f"{VIEWS_SQL.name}: SQL before the first CREATE at the beginning of a line")
    return statements


def upgrade() -> None:
    # Plan Deviation 5: an install that never ran M2's `migrate` would fail the SET NOT NULL at the end with a
    # bare constraint error; this guard names the remedy instead (no such install exists).
    op.execute("""\
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM active_config WHERE rubric_version_id IS NULL OR prompt_schema_version_id IS NULL) THEN
    RAISE EXCEPTION 'active_config has a NULL rubric_version_id or prompt_schema_version_id; run the M2 release''s `migrate` first (its seed sets both pointers), then upgrade again'
      USING ERRCODE = 'object_not_in_prerequisite_state';
  END IF;
END
$$;""")

    # Columns beyond the DDL on earlier tables (plan Deviations 2, 21 and 40)
    op.execute(
        "ALTER TABLE llm_spend_ledger ADD COLUMN admitted_buffer_usd numeric(10,2);   -- the buffer in force at admission; NULL on M2's rows (M2 Carry-forward, plan Deviation 2)"
    )
    op.execute(
        "ALTER TABLE jobs ADD COLUMN synthetic_eval boolean NOT NULL DEFAULT false;   -- an adversarial golden-case job (R2, plan Deviation 21)"
    )
    op.execute("CREATE INDEX jobs_synthetic_eval ON jobs (id) WHERE synthetic_eval;   -- plan Deviation 21")
    op.execute(
        "ALTER TABLE profile_versions ADD COLUMN synthetic_eval boolean NOT NULL DEFAULT false;   -- an adversarial golden-case profile (R2, plan Deviation 21)"
    )
    op.execute("""\
ALTER TABLE prompt_schema_versions ADD COLUMN parser_family text NOT NULL DEFAULT 'generator_v1'
  -- the member list is pinned by schemas.PARSER_FAMILIES (Task 4); the seed writes it (plan Deviation 40)
  CHECK (parser_family IN ('generator_v1', 'generator_v2_items'));""")

    # §7.6 Score cycles
    op.execute("""\
CREATE TABLE score_cycles (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  trigger              text NOT NULL CHECK (trigger IN ('periodic','score_now','batch_followup','manual')),
  llm_tuple_id         bigint NOT NULL REFERENCES llm_tuples(id),
  scope_version_id     bigint NOT NULL REFERENCES scope_versions(id),
  evaluation_policy_id bigint NOT NULL REFERENCES evaluation_policies(id),
  worker_epoch         bigint NOT NULL,
  status               text NOT NULL CHECK (status IN ('running','completed','failed')),
  status_reason        text,                                       -- e.g. 'orphaned', 'bug'
  bucket_usd_at_start  numeric(12,6) NOT NULL,                     -- §6.9.2 (may be negative)
  c_hat_usd            numeric(10,6) NOT NULL CHECK (c_hat_usd > 0),
  admission_size       integer NOT NULL CHECK (admission_size >= 0),   -- S
  admitted             jsonb NOT NULL DEFAULT '{}',                -- counts per lane
  spent_usd            numeric(12,6) NOT NULL DEFAULT 0,           -- counted spend of its attempts
  report               jsonb,
  started_at           timestamptz NOT NULL DEFAULT now(),
  heartbeat_at         timestamptz NOT NULL DEFAULT now(),
  finished_at          timestamptz,
  CHECK ((status = 'running') = (finished_at IS NULL)),
  CHECK (status <> 'failed' OR status_reason IS NOT NULL)
);""")
    op.execute(
        "CREATE UNIQUE INDEX score_cycles_one_running ON score_cycles ((true)) WHERE status = 'running';"
    )

    # §7.11 Scoring: bulk operations, targets, lane credits and attempts
    op.execute("""\
CREATE TABLE bulk_operations (                       -- §6.9.3
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind              text NOT NULL CHECK (kind = 'tuple_rescore'),
  llm_tuple_id      bigint NOT NULL REFERENCES llm_tuples(id),
  state             text NOT NULL CHECK (state IN ('active','completed','cancelled')),
  budget_usd        numeric(12,6) NOT NULL CHECK (budget_usd >= 0),
  spent_usd         numeric(12,6) NOT NULL DEFAULT 0      -- counted spend (reservations − credits)
                    CHECK (spent_usd >= 0),               -- plan Deviation 2 (no CHECK of spent_usd <= budget_usd: L1164)
  followup_held_usd numeric(12,6) NOT NULL DEFAULT 0 CHECK (followup_held_usd >= 0),
  created_at        timestamptz NOT NULL DEFAULT now(),
  finished_at       timestamptz,
  CHECK ((state = 'active') = (finished_at IS NULL))
);""")
    op.execute(
        "CREATE UNIQUE INDEX bulk_operations_one_active ON bulk_operations ((true)) WHERE state = 'active';"
    )
    op.execute(f"""\
CREATE TABLE score_targets (
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_revision_id         bigint NOT NULL REFERENCES job_revisions(id),
  llm_tuple_id            bigint NOT NULL REFERENCES llm_tuples(id),
  job_id                  bigint NOT NULL REFERENCES jobs(id),
  state                   text NOT NULL CHECK (state IN ('pending','filtered_out','deferred_duplicate',
                            'budget_deferred','in_progress','in_batch','published_accepted','published_contested',
                            'refused','failed_max_tokens','failed_invalid_output','failed_request',
                            'failed_transient','failed_attempts_exhausted','expired_unscored','cancelled')),
  state_reason            text,
  state_changed_at        timestamptz NOT NULL DEFAULT now(),
  override                text NOT NULL DEFAULT 'none'
                          CHECK (override IN ('none','score_now','score_anyway','individual')),
  override_at             timestamptz,
  similarity              real CHECK (similarity BETWEEN -1 AND 1),   -- NULL while awaiting embedding
  lane                    text CHECK (lane IN {LANE_VALUES_SQL}),   -- the list of LANE_VALUES_SQL, pinned by admission.LANE_VALUES (Task 12)
  always_include_reason   text CHECK (always_include_reason IN ('watchlist','title_match','full_text_twin',
                                                                'batch_followup')),
  attempt_no              integer NOT NULL DEFAULT 0 CHECK (attempt_no >= 0),     -- CAS counter, never reset
  charged_attempts        smallint NOT NULL DEFAULT 0 CHECK (charged_attempts >= 0), -- attempts that sent ≥ 1 call
  claim_id                uuid,
  claim_epoch             bigint,
  claimed_at              timestamptz,
  claim_heartbeat_at      timestamptz,
  batch_submission_id     bigint,                                     -- FK below
  deferred_group_id       bigint REFERENCES duplicate_groups(id) ON DELETE SET NULL,
  bulk_operation_id       bigint REFERENCES bulk_operations(id) ON DELETE SET NULL,
  not_before              timestamptz,                                -- transient backoff
  transient_failures      integer NOT NULL DEFAULT 0
                          CHECK (transient_failures >= 0),            -- A1 issue 4 (plan Deviation 2)
  refusal_category        text,
  filter_scope_version_id bigint REFERENCES scope_versions(id),
  resume_draft_id         bigint,                                     -- FK below (deferrable)
  published_result_id     bigint UNIQUE,                              -- FK below (deferrable)
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_revision_id, llm_tuple_id),
  CHECK ((state = 'in_progress') = (claim_id IS NOT NULL AND claim_epoch IS NOT NULL
                                    AND claim_heartbeat_at IS NOT NULL)),
  CHECK ((state = 'in_batch') = (batch_submission_id IS NOT NULL)),
  CHECK ((state = 'deferred_duplicate') = (deferred_group_id IS NOT NULL)),
  CHECK ((state IN ('published_accepted','published_contested')) = (published_result_id IS NOT NULL)),
  CHECK ((state = 'filtered_out') = (filter_scope_version_id IS NOT NULL)),
  CHECK ((override = 'none') = (override_at IS NULL)),
  CHECK (state <> 'refused' OR refusal_category IS NOT NULL),
  CHECK (state NOT LIKE 'failed_%' OR state_reason IS NOT NULL),
  CHECK (state NOT IN ('expired_unscored','cancelled','budget_deferred') OR state_reason IS NOT NULL)
);""")
    op.execute("""\
CREATE INDEX score_targets_admission ON score_targets (llm_tuple_id, state, similarity DESC NULLS LAST)
  WHERE state IN ('pending','budget_deferred');""")
    op.execute(
        "CREATE INDEX score_targets_oldest ON score_targets (llm_tuple_id, created_at) WHERE state = 'pending';"
    )
    op.execute(
        "CREATE INDEX score_targets_claims ON score_targets (claim_heartbeat_at) WHERE state = 'in_progress';"
    )
    op.execute(
        "CREATE INDEX score_targets_batch ON score_targets (batch_submission_id) WHERE state = 'in_batch';"
    )
    op.execute("CREATE INDEX score_targets_job ON score_targets (job_id);")
    op.execute(
        "CREATE INDEX score_targets_group ON score_targets (deferred_group_id) WHERE state = 'deferred_duplicate';"
    )
    op.execute(
        "CREATE INDEX score_targets_bulk ON score_targets (bulk_operation_id, state) WHERE bulk_operation_id IS NOT NULL;"
    )
    op.execute("""\
CREATE TABLE admission_lane_credits (               -- persistent fair rotation (§9.6.2)
  llm_tuple_id bigint NOT NULL REFERENCES llm_tuples(id),
  lane         text NOT NULL CHECK (lane IN ('similarity','oldest','exploration')),
  credit       numeric(10,4) NOT NULL DEFAULT 0 CHECK (credit >= -1),   -- a largest-remainder slot may borrow one
  PRIMARY KEY (llm_tuple_id, lane)
);""")
    op.execute(f"""\
CREATE TABLE score_attempts (
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  target_id               bigint NOT NULL REFERENCES score_targets(id),
  attempt_no              integer NOT NULL CHECK (attempt_no >= 1),
  mode                    text NOT NULL CHECK (mode IN ('online','batch')),
  claim_id                uuid,
  claim_epoch             bigint,
  cycle_id                bigint REFERENCES score_cycles(id) ON DELETE SET NULL,
  execution_config_id     bigint NOT NULL REFERENCES execution_configs(id),
  evaluation_policy_id    bigint NOT NULL REFERENCES evaluation_policies(id),
  lane                    text CHECK (lane IN {LANE_VALUES_SQL}),   -- the admission's lane (Codex r3 B15, r4 B2; plan Deviation 2); the list of LANE_VALUES_SQL, pinned by admission.LANE_VALUES (Task 12)
  outcome                 text NOT NULL CHECK (outcome IN ('running','published_accepted','published_contested',
                            'refused','failed_max_tokens','failed_invalid_output','failed_request','transient_error',
                            'budget_exhausted','claim_lost','cancelled','batch_draft_ready','submit_rejected','released')),
  outcome_detail          text,
  resumed_from_attempt_id bigint REFERENCES score_attempts(id),
  logical_calls           smallint NOT NULL DEFAULT 0 CHECK (logical_calls BETWEEN 0 AND 7),   -- §10.2: counted
                          -- with http_calls in the dispatch-marker transaction of a logical call's first try
  http_calls              smallint NOT NULL DEFAULT 0 CHECK (http_calls BETWEEN 0 AND 42),
  charged                 boolean NOT NULL DEFAULT false,      -- true once one LLM call was dispatched
  cost_usd                numeric(12,6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  started_at              timestamptz NOT NULL DEFAULT now(),
  ended_at                timestamptz,
  UNIQUE (target_id, attempt_no),
  CHECK ((outcome = 'running') = (ended_at IS NULL)),
  CHECK (logical_calls <= http_calls)
);""")
    # The foreign keys M2 staged on these tables (M2 Deviation 1)
    op.execute("ALTER TABLE llm_calls ADD FOREIGN KEY (attempt_id) REFERENCES score_attempts(id);")
    op.execute("ALTER TABLE llm_calls ADD FOREIGN KEY (target_id) REFERENCES score_targets(id);")
    op.execute(
        "ALTER TABLE llm_spend_ledger ADD FOREIGN KEY (bulk_operation_id) REFERENCES bulk_operations(id) ON DELETE SET NULL;"
    )

    # §7.11 Drafts, critic notes, results and score instances
    op.execute("""\
CREATE TABLE score_drafts (                          -- only structurally valid outputs are stored (§11.6)
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  attempt_id             bigint NOT NULL REFERENCES score_attempts(id),
  round                  smallint NOT NULL CHECK (round BETWEEN 0 AND 2),   -- 0 = generate, 1–2 = revisions
  llm_call_id            bigint NOT NULL UNIQUE REFERENCES llm_calls(id),
  output_json            jsonb NOT NULL,
  validation             jsonb NOT NULL,                    -- V1–V6 results incl. limit issues
  deterministic_failures text[] NOT NULL DEFAULT '{}',      -- criterion keys / fact_ids with V2/V3 failures
  provisional            jsonb NOT NULL,                    -- L, U, M, coverage under the active scope
  triggers               text[] NOT NULL DEFAULT '{}',      -- T0–T8 under the attempt's evaluation policy
  created_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (attempt_id, round)
);""")
    op.execute("""\
CREATE TABLE critic_notes (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  attempt_id       bigint NOT NULL REFERENCES score_attempts(id),
  round            smallint NOT NULL CHECK (round BETWEEN 1 AND 3),
  draft_id         bigint NOT NULL REFERENCES score_drafts(id),
  llm_call_id      bigint NOT NULL UNIQUE REFERENCES llm_calls(id),
  effort           text NOT NULL CHECK (effort IN ('low','medium','high','xhigh','max')),
  triggers         text[] NOT NULL,                         -- T0–T8 codes that caused this round
  verdict_returned text NOT NULL CHECK (verdict_returned IN ('accept','revise')),   -- advisory only
  derived_outcome  text NOT NULL CHECK (derived_outcome IN ('accept','revise','incomplete')),   -- §11.7.2
  review_json      jsonb NOT NULL,
  validation       jsonb NOT NULL,                          -- completeness, grounding, discarded issues
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (attempt_id, round)
);""")
    op.execute("""\
CREATE TABLE llm_results (
  id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  target_id                bigint NOT NULL UNIQUE,           -- ≤ 1 published per target; FK below (deferrable)
  attempt_id               bigint NOT NULL REFERENCES score_attempts(id),
  final_draft_id           bigint NOT NULL REFERENCES score_drafts(id),
  job_revision_id          bigint NOT NULL REFERENCES job_revisions(id),
  profile_version_id       bigint NOT NULL REFERENCES profile_versions(id),
  rubric_version_id        bigint NOT NULL REFERENCES rubric_versions(id),
  prompt_schema_version_id bigint NOT NULL REFERENCES prompt_schema_versions(id),
  model_policy_id          bigint NOT NULL REFERENCES model_policies(id),
  evaluation_policy_id     bigint NOT NULL REFERENCES evaluation_policies(id),   -- recorded, not identity
  status                   text NOT NULL CHECK (status IN ('accepted','contested')),
  criteria                 jsonb NOT NULL,          -- 6 LLM criteria: level|null, basis, rationale
  job_facts                jsonb NOT NULL,          -- facts with stable fact_ids (§11.4)
  knockout_candidates      jsonb NOT NULL,          -- derived (§11.4): requirement, comparison, review need
  fact_confirmations       jsonb NOT NULL,          -- {fact_id: structured | critic_confirmed | critic_rejected |
                                                    --           uncertain | unconfirmed}
  not_a_job_ad             boolean NOT NULL,
  injection_suspected      boolean NOT NULL,
  nulled_criteria          text[] NOT NULL DEFAULT '{}',
  truncated_fields         text[] NOT NULL DEFAULT '{}',     -- explicit, marked truncations (§11.6)
  critic_outcome           text NOT NULL CHECK (critic_outcome IN ('not_run','accepted','contested')),
  critic_injection         text CHECK (critic_injection IN ('none', 'present_no_influence', 'present_influenced',
                                                            'unavailable')),   -- the last valid review's assessment (Codex r1 #11, r2 B17; plan Deviation 2)
  revisions_used           smallint NOT NULL CHECK (revisions_used BETWEEN 0 AND 2),
  summary_en               text NOT NULL,
  summary_de               text NOT NULL,
  published_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_revision_id, profile_version_id, rubric_version_id, prompt_schema_version_id, model_policy_id),
  CHECK (status = 'contested' OR cardinality(nulled_criteria) = 0),
  CHECK ((critic_outcome = 'contested') = (status = 'contested')),
  CHECK ((critic_injection IS NULL) = (critic_outcome = 'not_run'))   -- plan Deviation 2
);""")
    op.execute(
        "ALTER TABLE llm_results ADD FOREIGN KEY (target_id) REFERENCES score_targets(id) DEFERRABLE INITIALLY DEFERRED;"
    )
    op.execute(
        "ALTER TABLE score_targets ADD FOREIGN KEY (published_result_id) REFERENCES llm_results(id) DEFERRABLE INITIALLY DEFERRED;"
    )
    op.execute(
        "ALTER TABLE score_targets ADD FOREIGN KEY (resume_draft_id) REFERENCES score_drafts(id) DEFERRABLE INITIALLY DEFERRED;"
    )
    op.execute("""\
CREATE TABLE result_evidence (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  llm_result_id   bigint NOT NULL REFERENCES llm_results(id) ON DELETE CASCADE,
  subject         text NOT NULL,          -- criterion key or fact_id ('code:subject')
  source_kind     text NOT NULL CHECK (source_kind IN ('description','structured_field','profile')),
  field_name      text,                   -- structured field (title, locations, work_mode, …) or profile section
  quote           text NOT NULL CHECK (length(quote) BETWEEN 4 AND 300),
  span_start      integer,                -- offsets into job_revisions.description_text
  span_end        integer,
  CHECK ((source_kind = 'description') = (span_start IS NOT NULL AND span_end > span_start)),
  CHECK ((source_kind = 'description') = (field_name IS NULL))
);""")
    op.execute("CREATE INDEX result_evidence_result ON result_evidence (llm_result_id);")
    op.execute("""\
CREATE TABLE det_results (                           -- immutable score instances (§11.11)
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  llm_result_id         bigint NOT NULL REFERENCES llm_results(id) ON DELETE CASCADE,
  scope_version_id      bigint NOT NULL REFERENCES scope_versions(id),
  job_id                bigint NOT NULL REFERENCES jobs(id),
  det_algorithm_version text NOT NULL,
  is_current            boolean NOT NULL DEFAULT true,
  superseded_at         timestamptz,
  det_criteria          jsonb NOT NULL,         -- location_work_mode, employment_salary (+ inputs)
  knockouts             jsonb NOT NULL,         -- applied codes, comparison result, severity, binding source
  industry_match        text NOT NULL CHECK (industry_match IN ('neutral','excluded','outside_include','unknown')),
  caps_applied          jsonb NOT NULL,
  score_low             numeric(5,2),
  score_mid             numeric(5,2),
  score_high            numeric(5,2),
  coverage              numeric(4,3) CHECK (coverage BETWEEN 0 AND 1),
  confidence            text CHECK (confidence IN ('high','medium','low')),
  det_inputs            jsonb NOT NULL,         -- job det-facts (with source ids) + scope fields used
  det_inputs_hash       bytea NOT NULL,
  computed_at           timestamptz NOT NULL DEFAULT now(),
  CHECK (is_current = (superseded_at IS NULL)),
  CHECK ((score_mid IS NULL) = (score_low IS NULL) AND (score_mid IS NULL) = (score_high IS NULL)),
  CHECK (score_mid IS NULL OR (0 <= score_low AND score_low <= score_mid AND score_mid <= score_high
                               AND score_high <= 100 AND score_mid = round((score_low + score_high) / 2, 2))),
  CHECK ((score_mid IS NULL) = (confidence IS NULL))       -- NULL only for verified not_a_job_ad
);""")
    op.execute(
        "CREATE UNIQUE INDEX det_results_current ON det_results (llm_result_id, scope_version_id) WHERE is_current;"
    )
    op.execute(
        "CREATE INDEX det_rank_mid ON det_results (scope_version_id, score_mid DESC NULLS LAST, job_id DESC) WHERE is_current;"
    )
    op.execute(
        "CREATE INDEX det_rank_low ON det_results (scope_version_id, score_low DESC NULLS LAST, job_id DESC) WHERE is_current;"
    )
    op.execute("CREATE INDEX det_results_job ON det_results (job_id, scope_version_id) WHERE is_current;")

    # §7.12 Batches
    op.execute("""\
CREATE TABLE batch_submissions (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  state               text NOT NULL CHECK (state IN ('preparing','submit_started','submitted','submit_rejected',
                                           'submit_ambiguous','released','ended','reconciled','abandoned')),
  llm_tuple_id        bigint NOT NULL REFERENCES llm_tuples(id),
  bulk_operation_id   bigint NOT NULL REFERENCES bulk_operations(id),
  operation_id        bigint REFERENCES operations(id) ON DELETE SET NULL,   -- the batch_submit operation that prepared it (Codex r1 #23; plan Deviation 27)
  operation_claim_id  uuid,                            -- that operation's execution claim (Codex r2 B12; plan Deviation 27)
  request_count       integer NOT NULL CHECK (request_count BETWEEN 1 AND 100000),
  request_hash        bytea NOT NULL,                  -- sha256 over ordered (custom_id, params hash)
  provider_batch_id   text UNIQUE,
  reserved_usd        numeric(12,6) NOT NULL CHECK (reserved_usd >= 0),   -- Σ of the items' ledger entries; numeric(12,6) (R7, plan Deviation 10)
  followup_held_usd   numeric(12,6) NOT NULL CHECK (followup_held_usd >= 0), -- §6.9.3 planning hold; numeric(12,6) (R7, plan Deviation 10)
  submit_started_at   timestamptz,
  submitted_at        timestamptz,
  expires_at          timestamptz,                     -- provider created_at + 24 h
  cancel_requested_at timestamptz,                     -- tuple superseded (§10.10)
  ended_at            timestamptz,
  results_complete_at timestamptz,                     -- the result stream was read to its end and validated
  reconciled_at       timestamptz,
  released_at         timestamptz,                     -- user "release and re-score" (spend stays ambiguous)
  follow_on_at        timestamptz,                     -- the follow-on submission was requested, once (Codex r2 B15)
  last_reconcile_at   timestamptz,
  reconcile_report    jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CHECK (state NOT IN ('submitted','ended','reconciled') OR provider_batch_id IS NOT NULL),
  CHECK (state IN ('preparing','abandoned') OR submit_started_at IS NOT NULL),
  CHECK (state <> 'reconciled' OR results_complete_at IS NOT NULL),
  CHECK ((state = 'released') <= (released_at IS NOT NULL)),
  CHECK (released_at IS NULL OR state IN ('released','ended','reconciled'))
);""")
    op.execute("""\
CREATE UNIQUE INDEX batch_one_in_flight ON batch_submissions ((true))
  WHERE state IN ('preparing','submit_started','submitted','submit_ambiguous','ended') AND released_at IS NULL;
-- A released submission is never in flight again: after the user's release new batches may be submitted,
-- and if its batch is found later it is reconciled (released → ended → reconciled, released_at kept)
-- outside this singleton, alongside the current submission (Codex R3-2, §10.10).""")
    op.execute(
        "ALTER TABLE score_targets ADD FOREIGN KEY (batch_submission_id) REFERENCES batch_submissions(id);"
    )
    op.execute("""\
CREATE TABLE batch_items (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  submission_id bigint NOT NULL REFERENCES batch_submissions(id),
  target_id     bigint NOT NULL REFERENCES score_targets(id),
  attempt_no    integer NOT NULL,
  custom_id     text NOT NULL UNIQUE CHECK (custom_id ~ '^[a-zA-Z0-9_-]{1,64}$'),   -- 't<target>_a<attempt>'
  llm_call_id   bigint UNIQUE,                                  -- FK below (deferrable)
  result_type   text NOT NULL DEFAULT 'awaiting' CHECK (result_type IN ('awaiting','succeeded','errored',
                                                                         'canceled','expired','missing')),
  stop_reason   text,
  decision      text CHECK (decision IN ('published','followup','refused','retry_online','failed_request',
                                         'failed_invalid_output','cancelled','ignored_fenced')),   -- §10.10 table
  processed_at  timestamptz,
  hold_usd         numeric(12,6) NOT NULL DEFAULT 0 CHECK (hold_usd >= 0),   -- the item's follow-up hold (Codex r1 #27; plan Deviation 27)
  hold_released_at timestamptz,                                              -- its once-only release (plan Deviation 27)
  UNIQUE (target_id, attempt_no),
  CHECK (custom_id = 't' || target_id || '_a' || attempt_no),
  CHECK ((result_type = 'awaiting') = (processed_at IS NULL)),
  CHECK ((processed_at IS NULL) = (decision IS NULL))
);""")
    op.execute(
        "ALTER TABLE batch_items ADD FOREIGN KEY (llm_call_id) REFERENCES llm_calls(id) DEFERRABLE INITIALLY DEFERRED;"
    )
    op.execute(
        "ALTER TABLE llm_calls ADD FOREIGN KEY (batch_item_id) REFERENCES batch_items(id) DEFERRABLE INITIALLY DEFERRED;"
    )

    # §7.13 User actions, feedback, calibration
    op.execute("""\
CREATE TABLE user_job_actions (
  job_id     bigint PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  seen_at    timestamptz,
  saved_at   timestamptz,
  applied_at timestamptz,
  hidden_at  timestamptz,
  note       text CHECK (length(note) <= 20000),   -- one API edit ≤ 2,000; merges concatenate (§8.6)
  updated_at timestamptz NOT NULL DEFAULT now()
);""")
    op.execute(
        "CREATE INDEX user_job_actions_saved ON user_job_actions (saved_at) WHERE saved_at IS NOT NULL;"
    )
    op.execute("""\
CREATE TABLE score_feedback (                        -- snapshot survives score and job retention
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id             bigint REFERENCES jobs(id) ON DELETE SET NULL,
  job_revision_id    bigint REFERENCES job_revisions(id) ON DELETE SET NULL,
  profile_version_id bigint REFERENCES profile_versions(id),     -- keeps the profile content from purge (§20.2)
  llm_result_id      bigint REFERENCES llm_results(id) ON DELETE SET NULL,
  det_result_id      bigint REFERENCES det_results(id) ON DELETE SET NULL,   -- the immutable instance labelled
  score_snapshot     jsonb,                          -- {L, M, U, coverage, confidence, tuple ids, scope id}
  kind               text NOT NULL CHECK (kind IN ('label','wrong_score')),
  label              text NOT NULL CHECK (label IN ('good','ok','poor')),
  comment            text CHECK (length(comment) <= 2000),
  created_at         timestamptz NOT NULL DEFAULT now()
);""")
    op.execute("""\
CREATE TABLE golden_cases (                          -- §11.13; its job inputs are retention-exempt (§20.1)
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_revision_id         bigint NOT NULL REFERENCES job_revisions(id),
  profile_version_id      bigint NOT NULL REFERENCES profile_versions(id),
  scope_version_id        bigint NOT NULL REFERENCES scope_versions(id),   -- the scope the label assumed
  label                   text NOT NULL CHECK (label IN ('good','ok','poor')),
  comment                 text CHECK (length(comment) <= 2000),
  expected_knockouts      text[] NOT NULL DEFAULT '{}',
  source                  text NOT NULL CHECK (source IN ('week1','feedback','adversarial')),
  labelled_from_result_id bigint REFERENCES llm_results(id) ON DELETE SET NULL,
  det_inputs_snapshot     jsonb NOT NULL,          -- frozen job det-facts at labelling time, each with its source ids
                                                   -- (the det_results.det_inputs format: salary, locations, …)
  det_input_sources       text[] NOT NULL,         -- every source_id the snapshot draws on (source purge, §20.5)
  is_canary               boolean NOT NULL DEFAULT false,
  active                  boolean NOT NULL DEFAULT true,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_revision_id, profile_version_id)        -- a second label upserts (ON CONFLICT DO UPDATE)
);""")
    op.execute("CREATE INDEX golden_cases_sources ON golden_cases USING gin (det_input_sources);")
    op.execute(
        "ALTER TABLE llm_calls ADD FOREIGN KEY (golden_case_id) REFERENCES golden_cases(id) ON DELETE CASCADE;"
    )
    op.execute("""\
CREATE TABLE eval_case_results (
  eval_run_id    bigint NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
  golden_case_id bigint NOT NULL REFERENCES golden_cases(id) ON DELETE CASCADE,
  repetition     smallint NOT NULL DEFAULT 1
                 CHECK (repetition BETWEEN 1 AND 3),   -- A1 issue 12 (plan Deviation 2)
  outcome        text NOT NULL CHECK (outcome IN ('accepted','contested','filtered_out','refused','failed')),
  critic_ran     boolean NOT NULL,
  llm_call_ids   bigint[] NOT NULL,
  output         jsonb NOT NULL,
  score_mid      numeric(5,2),
  PRIMARY KEY (eval_run_id, golden_case_id, repetition)
);""")

    # Plan Deviation 2 (M1's note 9 precedent): an index on each foreign key column that a retention or purge
    # delete reaches through its referencing side. (llm_calls.attempt_id has llm_calls_attempt, M2's.)
    op.execute(
        "CREATE INDEX llm_calls_target ON llm_calls (target_id) WHERE target_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX llm_calls_golden_case ON llm_calls (golden_case_id) WHERE golden_case_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX llm_calls_batch_item ON llm_calls (batch_item_id) WHERE batch_item_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX llm_spend_bulk ON llm_spend_ledger (bulk_operation_id) WHERE bulk_operation_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX score_attempts_cycle ON score_attempts (cycle_id) WHERE cycle_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX score_attempts_resumed ON score_attempts (resumed_from_attempt_id) WHERE resumed_from_attempt_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute("CREATE INDEX critic_notes_draft ON critic_notes (draft_id);   -- plan Deviation 2")
    op.execute("CREATE INDEX llm_results_attempt ON llm_results (attempt_id);   -- plan Deviation 2")
    op.execute("CREATE INDEX llm_results_final_draft ON llm_results (final_draft_id);   -- plan Deviation 2")
    op.execute(
        "CREATE INDEX score_targets_resume_draft ON score_targets (resume_draft_id) WHERE resume_draft_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX score_targets_filter_scope ON score_targets (filter_scope_version_id) WHERE filter_scope_version_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX score_targets_submission ON score_targets (batch_submission_id) WHERE batch_submission_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute("CREATE INDEX batch_items_submission ON batch_items (submission_id);   -- plan Deviation 2")
    op.execute(
        "CREATE INDEX det_results_result ON det_results (llm_result_id);   -- plan Deviation 2: the cascade from llm_results cannot use the partial det_results_current"
    )
    op.execute(
        "CREATE INDEX score_feedback_result ON score_feedback (llm_result_id) WHERE llm_result_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX score_feedback_det_result ON score_feedback (det_result_id) WHERE det_result_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX score_feedback_job ON score_feedback (job_id) WHERE job_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX score_feedback_revision ON score_feedback (job_revision_id) WHERE job_revision_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX golden_cases_labelled_from ON golden_cases (labelled_from_result_id) WHERE labelled_from_result_id IS NOT NULL;   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX golden_cases_profile ON golden_cases (profile_version_id);   -- plan Deviation 2"
    )
    op.execute("CREATE INDEX golden_cases_scope ON golden_cases (scope_version_id);   -- plan Deviation 2")
    op.execute(
        "CREATE INDEX eval_case_results_case ON eval_case_results (golden_case_id);   -- plan Deviation 2"
    )
    op.execute(
        "CREATE INDEX batch_submissions_operation ON batch_submissions (operation_id) WHERE operation_id IS NOT NULL;   -- plan Deviation 27"
    )

    # §7.18 The current-score views (db/views.sql; Deviation 21 adds one predicate there)
    for statement in view_statements():
        op.execute(statement)

    # §7.16 The defence-in-depth trigger of every M3 state machine (the edges: seed.sync_transition_matrix)
    op.execute(
        "CREATE TRIGGER score_targets_assert_transition BEFORE UPDATE OF state ON score_targets "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('state');"
    )
    op.execute(
        "CREATE TRIGGER score_attempts_assert_transition BEFORE UPDATE OF outcome ON score_attempts "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('outcome');"
    )
    op.execute(
        "CREATE TRIGGER score_cycles_assert_transition BEFORE UPDATE OF status ON score_cycles "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('status');"
    )
    op.execute(
        "CREATE TRIGGER bulk_operations_assert_transition BEFORE UPDATE OF state ON bulk_operations "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('state');"
    )
    op.execute(
        "CREATE TRIGGER batch_submissions_assert_transition BEFORE UPDATE OF state ON batch_submissions "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('state');"
    )
    op.execute(
        "CREATE TRIGGER batch_items_assert_transition BEFORE UPDATE OF result_type ON batch_items "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('result_type');"
    )

    # M0 Deviation 1, M2 plan Deviation 32: the spec's NOT NULL, last (the guard above ran first)
    op.execute(
        "ALTER TABLE active_config ALTER COLUMN rubric_version_id SET NOT NULL, "
        "ALTER COLUMN prompt_schema_version_id SET NOT NULL;"
    )


def downgrade() -> None:
    raise NotImplementedError("expand/contract: no downgrades")
