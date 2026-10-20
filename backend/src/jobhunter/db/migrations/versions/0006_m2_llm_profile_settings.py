"""M2 schema: the CV profile, the LLM tuple, the LLM spend ledger and calls, and eval runs.

Revision ID: 0006_m2_llm_profile_settings
Revises: 0005_m1_collection

Source: docs/superpowers/specs/2026-10-16-job-hunter-design.md, staged by
docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md (Task 1). The DDL below is copied verbatim from
the spec as static SQL, one `op.execute` per statement, in dependency order. Every foreign key to a table
that comes later is added afterwards with ALTER TABLE ... ADD FOREIGN KEY, as the spec does:

  §7.4   cv_files, profile_drafts (+ profile_drafts_one_open), profile_versions (+ profile_versions_live_hash),
         and the two deferrable foreign keys between profile_drafts and profile_versions
  §7.3   llm_tuples
  §7.5   query_proposals, and the foreign key queries.proposal_id -> query_proposals ON DELETE SET NULL
         (the one M1 Deviation 1 left out)
  §7.10  profile_vectors
  §7.11  llm_budget, llm_spend_ledger (+ llm_spend_month, llm_spend_unsettled), llm_calls (+ llm_calls_attempt,
         llm_calls_recent, llm_calls_cache), and the foreign keys profile_drafts.llm_call_id and
         query_proposals.llm_call_id -> llm_calls ON DELETE SET NULL
  §7.13  eval_runs, and the foreign key llm_calls.eval_run_id -> eval_runs ON DELETE CASCADE
  §7.3   the foreign keys M0 Deviation 2 left out: active_config.profile_version_id -> profile_versions,
         active_config.llm_tuple_id -> llm_tuples, runs.llm_tuple_id -> llm_tuples
  §7.14  is_fenced with its 'profile' branch (CREATE OR REPLACE over the M1 function), ending at the spec body:
         `p_tuple` now resolves to the profile version of that tuple
  §7.16  one assert_transition trigger per M2 state machine: profile_drafts(status), llm_spend_ledger(state),
         llm_calls(status), query_proposals(status), eval_runs(status); `migrate` writes their edges to
         transition_matrix from the code registry

These are the ONLY differences from the spec text. Each one carries a SQL comment naming its owner (plan
Deviations 1 and 2):

  1. Five columns keep their type, their CHECKs and their place in the CHECKs, but not their foreign key,
     because the referenced table is M3's. M3 adds each with ALTER TABLE ... ADD FOREIGN KEY, as the spec has it:
       llm_calls.attempt_id             -> score_attempts(id)
       llm_calls.target_id              -> score_targets(id)
       llm_calls.golden_case_id         -> golden_cases(id) ON DELETE CASCADE
       llm_calls.batch_item_id          -> batch_items(id) DEFERRABLE INITIALLY DEFERRED
       llm_spend_ledger.bulk_operation_id -> bulk_operations(id) ON DELETE SET NULL
     Until then nothing in M2 writes any of them (M2's calls are extraction, proposals and schema dry runs).
  2. profile_drafts.confirm_response jsonb, with CHECK (confirm_response IS NULL OR status = 'confirmed'): the
     stored body of a confirm, which a repeated confirm returns even under a new Idempotency-Key (§12.6).

active_config.rubric_version_id and prompt_schema_version_id stay nullable here: the migration that seeds the
rubric and the prompt schema restores NOT NULL (M0 Deviation 1).

Migrations are expand/contract: there is no downgrade.
"""

# ruff: noqa: E501  (the DDL is copied from the spec; its long lines stay as they are)

from collections.abc import Sequence

from alembic import op

revision: str = "0006_m2_llm_profile_settings"
down_revision: str | Sequence[str] | None = "0005_m1_collection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # §7.4 Profile and CV files
    op.execute("""\
CREATE TABLE cv_files (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sha256               bytea NOT NULL,
  mime                 text NOT NULL CHECK (mime IN ('application/pdf',
                        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                        'text/plain')),                     -- text/plain = pasted text
  size_bytes           integer NOT NULL CHECK (size_bytes BETWEEN 1 AND 10485760),
  page_count           smallint CHECK (page_count BETWEEN 1 AND 20),
  storage_path         text,                                 -- AES-256-GCM file on cv_store volume
  raw_text_enc         bytea,                                -- extracted text WITH contacts, AES-256-GCM
  stripped_text        text,                                 -- automatic contact-stripped text (§12.3)
  strip_report         jsonb,                                -- counts per category, no values
  extraction_method    text CHECK (extraction_method IN ('pypdf','pdfplumber','docx','pasted','native_pdf')),
  native_pdf_opt_in    boolean NOT NULL DEFAULT false,
  uploaded_at          timestamptz NOT NULL DEFAULT now(),
  purged_at            timestamptz,
  CHECK (purged_at IS NULL OR (storage_path IS NULL AND raw_text_enc IS NULL AND stripped_text IS NULL)),
  CHECK (NOT native_pdf_opt_in OR extraction_method = 'native_pdf')
);""")
    op.execute("""\
CREATE TABLE profile_drafts (
  id                           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  cv_file_id                   bigint NOT NULL REFERENCES cv_files(id),
  status                       text NOT NULL CHECK (status IN ('uploaded','extracting_text','text_review',
                                 'extracting_profile','ready','failed','confirmed','discarded')),
  status_reason                text,
  status_changed_at            timestamptz NOT NULL DEFAULT now(),
  reviewed_text                text,              -- the text the user approved; exactly what the LLM receives
  reviewed_text_sha256         bytea,
  native_pdf                   boolean NOT NULL DEFAULT false,   -- the user opted into sending the original PDF
  transmitted_text_sha256      bytea,             -- sha256 of the text actually sent with the extraction call
  draft_json                   jsonb,             -- editable profile (§12.4 schema)
  validation_issues            jsonb NOT NULL DEFAULT '[]',
  failure_code                 text,
  llm_call_id                  bigint,            -- FK added after llm_calls (SET NULL)
  confirmed_profile_version_id bigint,            -- FK below; the stored outcome of confirm (§12.6)
  confirm_response             jsonb,             -- the 201 body of confirm, returned again by a repeated confirm (plan Deviation 2: not in the spec DDL)
  created_at                   timestamptz NOT NULL DEFAULT now(),
  updated_at                   timestamptz NOT NULL DEFAULT now(),
  content_purged_at            timestamptz,       -- draft_json and reviewed_text removed (§20.1)
  CHECK (status NOT IN ('ready','confirmed') OR content_purged_at IS NOT NULL OR draft_json IS NOT NULL),
  CHECK (status NOT IN ('extracting_profile','ready') OR reviewed_text_sha256 IS NOT NULL OR native_pdf),
  CHECK (status <> 'failed' OR failure_code IS NOT NULL),
  CHECK ((status = 'confirmed') = (confirmed_profile_version_id IS NOT NULL)),
  CHECK (confirm_response IS NULL OR status = 'confirmed')   -- plan Deviation 2
);""")
    op.execute("""\
CREATE UNIQUE INDEX profile_drafts_one_open ON profile_drafts ((true))
  WHERE status IN ('uploaded','extracting_text','text_review','extracting_profile','ready');""")
    op.execute("""\
CREATE TABLE profile_versions (                      -- never deleted; content is nulled on purge
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_no   integer NOT NULL UNIQUE CHECK (version_no >= 1),
  draft_id     bigint NOT NULL UNIQUE,                       -- FK below (deferrable)
  cv_file_id   bigint REFERENCES cv_files(id) ON DELETE SET NULL,
  profile_json jsonb,                                        -- contact-stripped, immutable
  profile_hash bytea NOT NULL,                               -- see the partial unique index below
  confirmed_at timestamptz NOT NULL DEFAULT now(),
  purged_at    timestamptz,
  CHECK ((purged_at IS NULL) = (profile_json IS NOT NULL))
);""")
    op.execute("""\
ALTER TABLE profile_drafts ADD FOREIGN KEY (confirmed_profile_version_id) REFERENCES profile_versions(id)
  DEFERRABLE INITIALLY DEFERRED;""")
    op.execute(
        "ALTER TABLE profile_versions ADD FOREIGN KEY (draft_id) REFERENCES profile_drafts(id) DEFERRABLE INITIALLY DEFERRED;"
    )
    op.execute("""\
-- Identical content reuses only a USABLE version; a purged row never blocks or absorbs re-introduced content.
CREATE UNIQUE INDEX profile_versions_live_hash ON profile_versions (profile_hash) WHERE purged_at IS NULL;""")

    # §7.3 The LLM tuple
    op.execute("""\
CREATE TABLE llm_tuples (                            -- ids only; deleted only by delete-everything (§20.4)
  id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_version_id       bigint NOT NULL REFERENCES profile_versions(id),
  rubric_version_id        bigint NOT NULL REFERENCES rubric_versions(id),
  prompt_schema_version_id bigint NOT NULL REFERENCES prompt_schema_versions(id),
  model_policy_id          bigint NOT NULL REFERENCES model_policies(id),
  created_at               timestamptz NOT NULL DEFAULT now(),
  UNIQUE (profile_version_id, rubric_version_id, prompt_schema_version_id, model_policy_id)
);""")

    # §7.5 Query proposals (and the foreign key M1 left out of queries)
    op.execute("""\
CREATE TABLE query_proposals (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_version_id bigint NOT NULL REFERENCES profile_versions(id),
  status             text NOT NULL CHECK (status IN ('running','ready','failed','purged')),   -- purged: items removed (§20.2)
  items              jsonb,                          -- §11.12 schema output
  llm_call_id        bigint,                         -- FK added after llm_calls (SET NULL)
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (status <> 'ready' OR items IS NOT NULL)
);""")
    op.execute(
        "ALTER TABLE queries ADD FOREIGN KEY (proposal_id) REFERENCES query_proposals(id) ON DELETE SET NULL;   -- the FK M1 left out (M1 Deviation 1)"
    )

    # §7.10 Profile vectors (the embeddings table is M1's)
    op.execute("""\
CREATE TABLE profile_vectors (
  profile_version_id bigint NOT NULL REFERENCES profile_versions(id) ON DELETE CASCADE,
  kind               text NOT NULL CHECK (kind = 'summary' OR kind ~ '^title:[0-9]+$'),
  embedding_id       bigint NOT NULL REFERENCES embeddings(id),
  PRIMARY KEY (profile_version_id, kind)
);""")

    # §7.11 / §7.13 LLM spend, calls and eval runs. eval_runs comes before llm_calls, which references it.
    op.execute("""\
CREATE TABLE llm_budget (                            -- the serialisation point (§6.9.1)
  month                     date PRIMARY KEY CHECK (extract(day FROM month) = 1),   -- UTC calendar month
  cap_usd                   numeric(10,2) NOT NULL CHECK (cap_usd >= 0),
  overrun_buffer_usd        numeric(10,2) NOT NULL CHECK (overrun_buffer_usd >= 0),   -- never admitted (§6.9.1)
  counted_usd               numeric(12,6) NOT NULL DEFAULT 0,
  scoring_counted_usd       numeric(12,6) NOT NULL DEFAULT 0,
  nonscoring_counted_usd    numeric(12,6) NOT NULL DEFAULT 0,
  bucket_usd                numeric(12,6) NOT NULL,         -- may be negative (§6.9.2)
  bucket_at                 timestamptz NOT NULL,
  cap_lowered_at            timestamptz,
  counted_at_cap_change_usd numeric(12,6),
  cap_reached_alerted_at    timestamptz,
  updated_at                timestamptz NOT NULL DEFAULT now(),
  CHECK (counted_usd = scoring_counted_usd + nonscoring_counted_usd),
  CHECK ((cap_lowered_at IS NULL) = (counted_at_cap_change_usd IS NULL))
);""")
    op.execute("""\
CREATE TABLE eval_runs (
  id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind                     text NOT NULL CHECK (kind IN ('golden','canary','schema_dry_run')),
  route                    text NOT NULL CHECK (route IN ('production','forced_critic')),
  rubric_version_id        bigint REFERENCES rubric_versions(id),
  prompt_schema_version_id bigint REFERENCES prompt_schema_versions(id),
  model_policy_id          bigint REFERENCES model_policies(id),
  evaluation_policy_id     bigint REFERENCES evaluation_policies(id),
  execution_config_id      bigint REFERENCES execution_configs(id),
  repetitions              smallint NOT NULL DEFAULT 1 CHECK (repetitions BETWEEN 1 AND 3),
  status                   text NOT NULL CHECK (status IN ('running','completed','failed')),
  metrics                  jsonb,                     -- pooled and per-profile (§11.13)
  passed                   boolean,
  cost_usd                 numeric(10,4) NOT NULL DEFAULT 0,
  started_at               timestamptz NOT NULL DEFAULT now(),
  finished_at              timestamptz
);""")
    op.execute("""\
CREATE TABLE llm_spend_ledger (                      -- retained indefinitely; holds no content
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  budget_month       date NOT NULL REFERENCES llm_budget(month),
  kind               text NOT NULL CHECK (kind IN ('request','adjustment')),
  purpose            text NOT NULL CHECK (purpose IN ('generate','revise','critic','extract_profile',
                       'propose_queries','schema_dry_run')),
  eval_run           boolean NOT NULL DEFAULT false,              -- golden eval or canary (nonscoring)
  purpose_class      text NOT NULL CHECK (purpose_class IN ('scoring','nonscoring')),
  model_requested    text NOT NULL,
  state              text CHECK (state IN ('reserved','dispatched','billed','not_billed','ambiguous')),
  reserved_usd       numeric(12,6) NOT NULL CHECK (reserved_usd >= 0),
  settled_usd        numeric(12,6) CHECK (settled_usd >= 0),
  input_tokens_bound integer CHECK (input_tokens_bound > 0),   -- §10.7
  batch              boolean NOT NULL DEFAULT false,
  bulk_operation_id  bigint,                                   -- M3 adds REFERENCES bulk_operations(id) ON DELETE SET NULL (plan Deviation 1)
  admitted_cap_usd   numeric(10,2) NOT NULL,                   -- audit of the admission decision
  counted_before_usd numeric(12,6) NOT NULL,
  evidence           text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  dispatched_at      timestamptz,
  settled_at         timestamptz,
  CHECK ((kind = 'request') = (state IS NOT NULL)),
  CHECK (state NOT IN ('billed','not_billed') OR settled_usd IS NOT NULL),
  CHECK (state <> 'not_billed' OR (settled_usd = 0 AND evidence IN ('error_response_no_content',
           'connect_failed_before_send','never_dispatched','batch_submit_rejected','batch_item_errored',
           'batch_item_canceled','batch_item_expired'))),
  CHECK (state <> 'billed' OR settled_usd <= reserved_usd OR evidence = 'reservation_bound_violated'),
  CHECK (purpose_class = 'scoring' OR bulk_operation_id IS NULL),
  CHECK (NOT eval_run OR purpose_class = 'nonscoring'),
  CHECK (purpose IN ('generate','revise','critic') OR purpose_class = 'nonscoring')
);""")
    op.execute("CREATE INDEX llm_spend_month ON llm_spend_ledger (budget_month, state);")
    op.execute("""\
CREATE INDEX llm_spend_unsettled ON llm_spend_ledger (state, created_at) WHERE state IN ('reserved','dispatched');
-- counted(month) = Σ settled_usd (billed) + Σ reserved_usd (reserved, dispatched, ambiguous) + Σ adjustments;
-- it must equal llm_budget.counted_usd (heavy reconciler, §6.9.1).""")
    op.execute("""\
CREATE TABLE llm_calls (                             -- content-bearing; deleted with its owner (§20.2)
  id                          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  purpose                     text NOT NULL CHECK (purpose IN ('generate','revise','critic','extract_profile',
                                'propose_queries','schema_dry_run')),
  attempt_id                  bigint,                         -- M3 adds REFERENCES score_attempts(id) (plan Deviation 1)
  target_id                   bigint,                         -- M3 adds REFERENCES score_targets(id) (plan Deviation 1)
  round                       smallint CHECK (round BETWEEN 0 AND 3),
  logical_call_no             smallint CHECK (logical_call_no BETWEEN 1 AND 7),
  max_tokens_stage            text NOT NULL DEFAULT 'first' CHECK (max_tokens_stage IN ('first','enlarged')),
  transport_try               smallint NOT NULL DEFAULT 1 CHECK (transport_try BETWEEN 1 AND 3),
  profile_draft_id            bigint REFERENCES profile_drafts(id) ON DELETE SET NULL,
  eval_run_id                 bigint,                         -- FK below
  golden_case_id              bigint,                         -- eval and canary calls (§20.2); M3 adds REFERENCES golden_cases(id) ON DELETE CASCADE (plan Deviation 1)
  execution_config_id         bigint NOT NULL REFERENCES execution_configs(id),
  model_requested             text NOT NULL CHECK (model_requested IN ('claude-opus-5-5','claude-sonnet-5-5',
                                                                       'claude-haiku-5-5','claude-fable-5-1')),
  model_served                text,
  effort                      text NOT NULL CHECK (effort IN ('low','medium','high','xhigh','max')),
  max_tokens                  integer NOT NULL CHECK (max_tokens BETWEEN 256 AND 64000),
  transport                   text NOT NULL CHECK (transport IN ('stream','batch')),
  batch_item_id               bigint,                         -- M3 adds REFERENCES batch_items(id) DEFERRABLE INITIALLY DEFERRED (plan Deviation 1)
  prompt_template_version     text NOT NULL,
  rendered_prompt_sha256      bytea NOT NULL,
  prefix_sha256               bytea,                          -- cached prefix bytes (cache monitor key)
  input_refs                  jsonb NOT NULL,                 -- ids only: job_revision, profile_version, draft…
  input_tokens_bound          integer NOT NULL CHECK (input_tokens_bound > 0),
  status                      text NOT NULL CHECK (status IN ('reserved','dispatched','completed','error',
                                                              'ambiguous','cancelled')),
  http_status                 smallint,
  error_code                  text,
  stop_reason                 text,
  refusal_category            text,
  provider_request_id         text,
  input_tokens                integer,
  cache_creation_input_tokens integer,
  cache_read_input_tokens     integer,
  output_tokens               integer,
  cost_usd                    numeric(12,6),
  spend_ledger_id             bigint NOT NULL UNIQUE REFERENCES llm_spend_ledger(id),
  output_json                 jsonb,                          -- validated structured output (no request body)
  started_at                  timestamptz NOT NULL DEFAULT now(),
  first_event_at              timestamptz,
  ended_at                    timestamptz,
  CHECK (purpose NOT IN ('generate','revise','critic') OR eval_run_id IS NOT NULL
         OR (attempt_id IS NOT NULL AND target_id IS NOT NULL
             AND (transport = 'batch' OR logical_call_no IS NOT NULL))),
  CHECK (status <> 'completed' OR (stop_reason IS NOT NULL AND output_tokens IS NOT NULL))
);""")
    op.execute("CREATE INDEX llm_calls_attempt ON llm_calls (attempt_id);")
    op.execute("CREATE INDEX llm_calls_recent ON llm_calls (started_at DESC);")
    op.execute("CREATE INDEX llm_calls_cache ON llm_calls (model_served, effort, purpose, started_at DESC);")
    op.execute(
        "ALTER TABLE profile_drafts ADD FOREIGN KEY (llm_call_id) REFERENCES llm_calls(id) ON DELETE SET NULL;"
    )
    op.execute(
        "ALTER TABLE query_proposals ADD FOREIGN KEY (llm_call_id) REFERENCES llm_calls(id) ON DELETE SET NULL;"
    )
    op.execute(
        "ALTER TABLE llm_calls ADD FOREIGN KEY (eval_run_id) REFERENCES eval_runs(id) ON DELETE CASCADE;"
    )

    # §7.3 The pointers M0 left without a foreign key (M0 Deviation 2)
    op.execute(
        "ALTER TABLE active_config ADD FOREIGN KEY (profile_version_id) REFERENCES profile_versions(id);"
    )
    op.execute("ALTER TABLE active_config ADD FOREIGN KEY (llm_tuple_id) REFERENCES llm_tuples(id);")
    op.execute("ALTER TABLE runs ADD FOREIGN KEY (llm_tuple_id) REFERENCES llm_tuples(id);")

    # §7.14 The purge fence of a profile
    op.execute("""\
CREATE OR REPLACE FUNCTION is_fenced(p_job bigint, p_tuple bigint DEFAULT NULL) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM purge_fences f
     WHERE f.released_at IS NULL
       AND (f.kind = 'global'
            OR (f.kind = 'jobs' AND p_job = ANY (f.job_ids))
            OR (f.kind = 'source' AND (
                  p_job = ANY (f.job_ids)                  -- the stored affected set, independent of lineage
               OR EXISTS (SELECT 1 FROM postings p JOIN source_instances si ON si.id = p.source_instance_id
                           WHERE p.job_id = p_job AND si.source_id = f.source_id)
               OR EXISTS (SELECT 1 FROM job_revisions r JOIN job_revision_sources s ON s.job_revision_id = r.id
                           WHERE r.job_id = p_job AND s.source_id = f.source_id)))
            OR (f.kind = 'profile' AND EXISTS (SELECT 1 FROM llm_tuples t
                                                WHERE t.id = p_tuple
                                                  AND t.profile_version_id = f.profile_version_id))));
$$;                                                -- callers hold the job row FOR SHARE (§20.2)""")

    # §7.16 The defence-in-depth trigger of every M2 state machine (the edges: seed.sync_transition_matrix)
    op.execute(
        "CREATE TRIGGER profile_drafts_assert_transition BEFORE UPDATE OF status ON profile_drafts "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('status');"
    )
    op.execute(
        "CREATE TRIGGER llm_spend_ledger_assert_transition BEFORE UPDATE OF state ON llm_spend_ledger "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('state');"
    )
    op.execute(
        "CREATE TRIGGER llm_calls_assert_transition BEFORE UPDATE OF status ON llm_calls "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('status');"
    )
    op.execute(
        "CREATE TRIGGER query_proposals_assert_transition BEFORE UPDATE OF status ON query_proposals "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('status');"
    )
    op.execute(
        "CREATE TRIGGER eval_runs_assert_transition BEFORE UPDATE OF status ON eval_runs "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('status');"
    )


def downgrade() -> None:
    raise NotImplementedError("expand/contract: no downgrades")
