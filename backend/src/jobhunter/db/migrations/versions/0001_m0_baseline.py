"""M0 baseline schema.

Revision ID: 0001_m0_baseline
Revises: none

Source: docs/superpowers/specs/2026-10-16-job-hunter-design.md. The DDL below is copied verbatim from the
spec as static SQL, one `op.execute` per statement, in dependency order:

  §7.1   the three CREATE EXTENSION IF NOT EXISTS statements, immutable_unaccent, german_unaccent
         (CREATE and ALTER MAPPING), ranking_state
  §7.2   users, sessions (+ sessions_expiry), login_attempts (+ login_attempts_ip, login_attempts_fail),
         login_lockouts, api_idempotency
  §7.3   scope_versions, model_policies, execution_configs, evaluation_policies, rubric_versions,
         prompt_schema_versions, settings_versions, active_config, schedule_state, operations
         (+ operations_live_dedup, operations_pending)
  §7.6   runs (+ runs_slot_uq, runs_one_active_collect, runs_running_hb, runs_recent), run_attempts,
         worker_heartbeat
  §7.14  alerts (+ alerts_open_dedup), purge_fences (+ purge_fences_active, purge_fences_jobs), is_fenced

Not created here (their tables arrive in later milestones): llm_tuples, profile_versions, score_cycles,
sources, postings, deletion_manifests and the rest of §7.4 to §7.14.

These are the ONLY differences from the spec text. Each one carries a SQL comment naming its owner milestone
(plan Deviations 1 and 2):

  1. active_config.rubric_version_id and active_config.prompt_schema_version_id are nullable (the spec says
     NOT NULL; the foreign keys to rubric_versions and prompt_schema_versions stay). M2 seeds the rows and sets
     the pointers (the seed step of `migrate`), and M3's first migration restores NOT NULL (M2 plan Deviation 32).
  2. These foreign keys are not created, because their target tables do not exist yet; the columns keep their
     type and their CHECK constraints. M2 adds them with ALTER TABLE ... ADD FOREIGN KEY:
       active_config.profile_version_id -> profile_versions
       active_config.llm_tuple_id       -> llm_tuples
       runs.llm_tuple_id                -> llm_tuples
  3. purge_fences.source_id -> sources is added by M1, and purge_fences.manifest_id -> deletion_manifests
     (the column stays NOT NULL) by M5.
  4. is_fenced(p_job bigint, p_tuple bigint DEFAULT NULL) has the 'global' and 'jobs' branches only (p_tuple
     is unused until then). M1 adds the 'source' branch and M2 the 'profile' branch with CREATE OR REPLACE,
     ending at the §7.14 body.

Migrations are expand/contract: there is no downgrade.
"""

# ruff: noqa: E501  (the DDL is copied from the spec; its long lines stay as they are)

from collections.abc import Sequence

from alembic import op

revision: str = "0001_m0_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # §7.1 Conventions and extensions
    op.execute(
        "CREATE EXTENSION IF NOT EXISTS vector;            -- all three created by deploy/postgres/init (§19.3): no-ops here"
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent;")
    op.execute("""\
CREATE FUNCTION immutable_unaccent(text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
  AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, $1) $$;""")
    op.execute("CREATE TEXT SEARCH CONFIGURATION german_unaccent (COPY = german);")
    op.execute("""\
ALTER TEXT SEARCH CONFIGURATION german_unaccent
  ALTER MAPPING FOR hword, hword_part, word WITH unaccent, german_stem;""")
    op.execute("""\
CREATE TABLE ranking_state (                       -- §14.3 pagination generation
  id         smallint PRIMARY KEY CHECK (id = 1),
  generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
  updated_at timestamptz NOT NULL DEFAULT now()
);""")

    # §7.2 Identity, sessions, throttling
    op.execute("""\
CREATE TABLE users (
  id                  smallint PRIMARY KEY CHECK (id = 1),          -- single user
  username            text NOT NULL UNIQUE CHECK (length(username) BETWEEN 3 AND 64),
  password_hash       text NOT NULL,                                -- argon2id PHC string
  totp_secret_enc     bytea,                                        -- AES-256-GCM (data key)
  totp_enabled        boolean NOT NULL DEFAULT false,
  password_changed_at timestamptz NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CHECK (NOT totp_enabled OR totp_secret_enc IS NOT NULL)
);""")
    op.execute("""\
CREATE TABLE sessions (
  token_hmac          bytea PRIMARY KEY,                            -- HMAC-SHA256(token, session pepper)
  user_id             smallint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at          timestamptz NOT NULL DEFAULT now(),
  last_seen_at        timestamptz NOT NULL DEFAULT now(),
  idle_expires_at     timestamptz NOT NULL,                         -- last_seen + 12 h
  absolute_expires_at timestamptz NOT NULL,                         -- created + 30 d
  ip                  inet,                                         -- real client address (§16.3)
  user_agent_sha256   bytea,
  revoked_at          timestamptz,
  CHECK (idle_expires_at <= absolute_expires_at)
);""")
    op.execute("CREATE INDEX sessions_expiry ON sessions (absolute_expires_at);")
    op.execute("""\
CREATE TABLE login_attempts (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at            timestamptz NOT NULL DEFAULT now(),
  ip            inet NOT NULL,
  username_hmac bytea NOT NULL,                                     -- never the raw username
  success       boolean NOT NULL
);""")
    op.execute("CREATE INDEX login_attempts_ip ON login_attempts (ip, at DESC);")
    op.execute("CREATE INDEX login_attempts_fail ON login_attempts (at DESC) WHERE NOT success;")
    op.execute("""\
CREATE TABLE login_lockouts (
  scope        text PRIMARY KEY CHECK (scope = 'global' OR scope LIKE 'ip:%'),
  locked_until timestamptz NOT NULL,
  reason       text NOT NULL
);""")
    op.execute("""\
CREATE TABLE api_idempotency (                                      -- §14.1
  route            text NOT NULL,                                   -- e.g. 'POST /api/runs'
  idem_key         text NOT NULL CHECK (idem_key ~ '^[A-Za-z0-9_-]{16,128}$'),
  request_sha256   bytea NOT NULL,                                  -- canonical body + path params
  status           text NOT NULL CHECK (status IN ('in_progress','completed')),
  response_status  smallint,
  response_body    jsonb,                                           -- ids and states only, no PII
  created_at       timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL,                            -- created + 7 d
  PRIMARY KEY (route, idem_key),
  CHECK ((status = 'completed') = (response_status IS NOT NULL))
);""")

    # §7.3 Configuration, versions, active pointers, operations
    op.execute("""\
CREATE TABLE scope_versions (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scope_hash  bytea NOT NULL UNIQUE,                 -- sha256 of canonical JSON (sorted keys)
  payload     jsonb NOT NULL,                        -- resolved scope (§13.1, group "Scope")
  created_at  timestamptz NOT NULL DEFAULT now()
);""")
    op.execute("""\
CREATE TABLE model_policies (                        -- WHICH models score: part of the LLM tuple
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  policy_hash bytea NOT NULL UNIQUE,                 -- sha256 over the whole payload
  payload     jsonb NOT NULL,                        -- {node: {model, effort}} for every node
  created_at  timestamptz NOT NULL DEFAULT now()
);""")
    op.execute("""\
CREATE TABLE execution_configs (                     -- HOW calls run: not part of the tuple
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  config_hash bytea NOT NULL UNIQUE,                 -- sha256 over the whole payload
  payload     jsonb NOT NULL,                        -- {node: {max_tokens, max_tokens_retry, deadline_s}}
  created_at  timestamptz NOT NULL DEFAULT now()
);""")
    op.execute("""\
CREATE TABLE evaluation_policies (                   -- critic routing (§11.7): not part of the tuple
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_no  integer NOT NULL UNIQUE CHECK (version_no >= 1),
  policy_hash bytea NOT NULL UNIQUE,                 -- sha256 over the whole payload
  payload     jsonb NOT NULL,                        -- thresholds, sample rate, efforts (§11.7.4)
  projected_critic_rate numeric(4,3) NOT NULL CHECK (projected_critic_rate BETWEEN 0 AND 1),
  created_at  timestamptz NOT NULL DEFAULT now()
);""")
    op.execute("""\
CREATE TABLE rubric_versions (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version      text NOT NULL UNIQUE,                 -- 'v1'
  content_hash bytea NOT NULL UNIQUE,
  weights      jsonb NOT NULL,                       -- validated: 8 criteria, integers, sum = 100
  anchors      jsonb NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);""")
    op.execute("""\
CREATE TABLE prompt_schema_versions (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  prompt_schema_hash bytea NOT NULL UNIQUE,          -- sha256(templates ‖ JSON schemas ‖ renderer version)
  templates          jsonb NOT NULL,
  schemas            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now()
);""")
    op.execute("""\
CREATE TABLE settings_versions (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_no            integer NOT NULL UNIQUE CHECK (version_no >= 1),
  schema_version        smallint NOT NULL,
  payload               jsonb NOT NULL,            -- full validated settings (§13)
  scope_version_id      bigint NOT NULL REFERENCES scope_versions(id),
  model_policy_id       bigint NOT NULL REFERENCES model_policies(id),
  execution_config_id   bigint NOT NULL REFERENCES execution_configs(id),
  evaluation_policy_id  bigint NOT NULL REFERENCES evaluation_policies(id),
  base_version_id       bigint REFERENCES settings_versions(id) ON DELETE SET NULL,   -- optimistic concurrency
  effect_classes        text[] NOT NULL CHECK (effect_classes <@ ARRAY['none','schedule','collection',
                          'admission','budget','scope','execution','evaluation_policy','llm_tuple']),
  acknowledged_warnings text[] NOT NULL DEFAULT '{}',
  eval_override_ack     boolean NOT NULL DEFAULT false,              -- §11.13 gate override
  created_at            timestamptz NOT NULL DEFAULT now()
);""")
    op.execute("""\
CREATE TABLE active_config (
  id                       smallint PRIMARY KEY CHECK (id = 1),
  settings_version_id      bigint NOT NULL REFERENCES settings_versions(id),
  scope_version_id         bigint NOT NULL REFERENCES scope_versions(id),   -- joined by current-score view
  pending_scope_version_id bigint REFERENCES scope_versions(id),            -- precompute in progress
  profile_version_id       bigint,                                          -- NULL before first CV; M2 adds REFERENCES profile_versions(id)
  rubric_version_id        bigint REFERENCES rubric_versions(id),           -- nullable in M0: M2 seeds the row and restores NOT NULL
  prompt_schema_version_id bigint REFERENCES prompt_schema_versions(id),   -- nullable in M0: M2 seeds the row and restores NOT NULL
  model_policy_id          bigint NOT NULL REFERENCES model_policies(id),
  execution_config_id      bigint NOT NULL REFERENCES execution_configs(id),
  evaluation_policy_id     bigint NOT NULL REFERENCES evaluation_policies(id),
  llm_tuple_id             bigint,                                          -- M2 adds REFERENCES llm_tuples(id)
  updated_at               timestamptz NOT NULL DEFAULT now(),
  CHECK ((profile_version_id IS NULL) = (llm_tuple_id IS NULL)),
  CHECK (pending_scope_version_id IS DISTINCT FROM scope_version_id)
);""")
    op.execute("""\
CREATE TABLE schedule_state (
  id                  smallint PRIMARY KEY CHECK (id = 1),
  anchor_utc          timestamptz NOT NULL,
  interval_s          integer NOT NULL CHECK (interval_s BETWEEN 3600 AND 604800),
  enabled             boolean NOT NULL,
  last_evaluated_slot timestamptz,
  settings_version_id bigint NOT NULL REFERENCES settings_versions(id),
  updated_at          timestamptz NOT NULL DEFAULT now()
);""")
    op.execute("""\
CREATE TABLE operations (                            -- durable intents (§3.3.2)
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind            text NOT NULL CHECK (kind IN ('extract_profile_text','extract_profile','embed_profile',
                    'propose_queries','apply_scope','create_targets_for_tuple','batch_submit','export',
                    'purge','eval_run','watchlist_validate')),
  payload         jsonb NOT NULL,                    -- ids only, never content
  dedup_key       text NOT NULL,                     -- e.g. 'apply_scope:<scope_version_id>'
  status          text NOT NULL CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
  claim_id        uuid,                              -- fresh per execution; the ownership token (§3.3.3)
  worker_epoch    bigint,                            -- owner epoch while running
  attempts        smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 10),
  not_before      timestamptz,
  heartbeat_at    timestamptz,
  progress        jsonb NOT NULL DEFAULT '{}',       -- {done, total}
  last_error_code text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  started_at      timestamptz,
  finished_at     timestamptz,
  CHECK ((status = 'running') = (claim_id IS NOT NULL AND worker_epoch IS NOT NULL AND heartbeat_at IS NOT NULL)),
  CHECK (status NOT IN ('succeeded','failed','cancelled') OR finished_at IS NOT NULL),
  CHECK (status <> 'failed' OR last_error_code IS NOT NULL)
);""")
    op.execute(
        "CREATE UNIQUE INDEX operations_live_dedup ON operations (kind, dedup_key) WHERE status IN ('pending','running');"
    )
    op.execute("CREATE INDEX operations_pending ON operations (created_at) WHERE status = 'pending';")

    # §7.6 Runs, worker heartbeat
    op.execute("""\
CREATE TABLE runs (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind                text NOT NULL DEFAULT 'collect' CHECK (kind = 'collect'),
  trigger             text NOT NULL CHECK (trigger IN ('schedule','catch_up','manual')),
  scheduled_slot      timestamptz,
  status              text NOT NULL CHECK (status IN ('queued','running','succeeded','partial','failed',
                                           'failed_invariant','cancelled','missed','skipped_overlap')),
  include_paid        boolean NOT NULL,
  settings_version_id bigint NOT NULL REFERENCES settings_versions(id),
  scope_version_id    bigint NOT NULL REFERENCES scope_versions(id),
  llm_tuple_id        bigint,                                      -- M2 adds REFERENCES llm_tuples(id)
  attempt_no          integer NOT NULL DEFAULT 0 CHECK (attempt_no >= 0),
  current_thread_id   text,
  resume_count        integer NOT NULL DEFAULT 0 CHECK (resume_count BETWEEN 0 AND 4),
  queued_at           timestamptz NOT NULL DEFAULT now(),
  started_at          timestamptz,
  heartbeat_at        timestamptz,
  finished_at         timestamptz,
  status_reason       text,
  report              jsonb,                                       -- §17.3 run report
  CHECK ((trigger = 'manual') = (scheduled_slot IS NULL)),
  CHECK (status NOT IN ('missed','skipped_overlap') OR (attempt_no = 0 AND started_at IS NULL)),
  CHECK (status <> 'running' OR (started_at IS NOT NULL AND heartbeat_at IS NOT NULL
                                 AND current_thread_id = 'run-' || id || '-a' || attempt_no)),
  CHECK (status NOT IN ('succeeded','partial','failed','failed_invariant','cancelled') OR finished_at IS NOT NULL)
);""")
    op.execute(
        "CREATE UNIQUE INDEX runs_slot_uq ON runs (kind, scheduled_slot) WHERE scheduled_slot IS NOT NULL;"
    )
    op.execute("""\
CREATE UNIQUE INDEX runs_one_active_collect ON runs ((true))
  WHERE kind = 'collect' AND status IN ('queued','running');""")
    op.execute("CREATE INDEX runs_running_hb ON runs (heartbeat_at) WHERE status = 'running';")
    op.execute("CREATE INDEX runs_recent ON runs (queued_at DESC, id DESC);")
    op.execute("""\
CREATE TABLE run_attempts (
  run_id       bigint NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  attempt_no   integer NOT NULL CHECK (attempt_no >= 1),
  worker_epoch bigint NOT NULL,                                    -- owner epoch (§3.3.1)
  thread_id    text NOT NULL UNIQUE,
  started_at   timestamptz NOT NULL DEFAULT now(),
  ended_at     timestamptz,
  end_reason   text CHECK (end_reason IN ('completed','heartbeat_stale','worker_takeover','cancelled','failed')),
  PRIMARY KEY (run_id, attempt_no),
  CHECK (thread_id = 'run-' || run_id || '-a' || attempt_no)
);""")
    op.execute("""\
CREATE TABLE worker_heartbeat (
  id                 smallint PRIMARY KEY CHECK (id = 1),
  worker_instance_id uuid NOT NULL,
  worker_epoch       bigint NOT NULL DEFAULT 0 CHECK (worker_epoch >= 0),
  hostname           text NOT NULL,
  pid                integer NOT NULL,
  lock_backend_pid   integer NOT NULL,
  app_version        text NOT NULL,
  started_at         timestamptz NOT NULL,
  heartbeat_at       timestamptz NOT NULL
);""")

    # §7.14 Operations: alerts, purge fences
    op.execute("""\
CREATE TABLE alerts (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code             text NOT NULL,                  -- §17.6 catalogue
  severity         text NOT NULL CHECK (severity IN ('info','warn','critical')),
  dedup_key        text NOT NULL,
  message          text NOT NULL,                  -- no PII
  context          jsonb NOT NULL DEFAULT '{}',    -- ids and counts only
  first_at         timestamptz NOT NULL DEFAULT now(),
  last_at          timestamptz NOT NULL DEFAULT now(),
  occurrences      integer NOT NULL DEFAULT 1 CHECK (occurrences >= 1),
  acknowledged_at  timestamptz,
  resolved_at      timestamptz,
  last_notified_at timestamptz,
  notify_failures  integer NOT NULL DEFAULT 0
);""")
    op.execute("CREATE UNIQUE INDEX alerts_open_dedup ON alerts (dedup_key) WHERE resolved_at IS NULL;")
    op.execute("""\
CREATE TABLE purge_fences (                        -- §20.2; persisted, survives restarts
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind        text NOT NULL CHECK (kind IN ('global','source','profile','jobs')),
  source_id   text,                                -- M1 adds REFERENCES sources(id)
  profile_version_id bigint,                       -- no FK: the row outlives the purge it fences
  job_ids     bigint[] NOT NULL DEFAULT '{}',          -- jobs: the batch; source: the stored affected set (§19.5)
  manifest_id bigint NOT NULL,                     -- M5 adds REFERENCES deletion_manifests(id)
  created_at  timestamptz NOT NULL DEFAULT now(),
  released_at timestamptz,                         -- set in the purge's final transaction
  CHECK ((kind = 'source') = (source_id IS NOT NULL)),
  CHECK ((kind = 'profile') = (profile_version_id IS NOT NULL)),
  CHECK (kind <> 'jobs' OR cardinality(job_ids) >= 1)
);""")
    op.execute("CREATE INDEX purge_fences_active ON purge_fences (kind) WHERE released_at IS NULL;")
    op.execute(
        "CREATE INDEX purge_fences_jobs ON purge_fences USING gin (job_ids) WHERE released_at IS NULL;"
    )
    op.execute("""\
CREATE FUNCTION is_fenced(p_job bigint, p_tuple bigint DEFAULT NULL) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM purge_fences f WHERE f.released_at IS NULL
                   AND (f.kind = 'global' OR (f.kind = 'jobs' AND p_job = ANY (f.job_ids))));
$$;  -- M1 adds the 'source' branch, M2 the 'profile' branch (CREATE OR REPLACE to the §7.14 text)""")


def downgrade() -> None:
    raise NotImplementedError("expand/contract: no downgrades")
