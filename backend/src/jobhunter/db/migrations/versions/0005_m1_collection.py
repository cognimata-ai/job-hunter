"""M1 collection schema: sources, receipts, quota, postings, jobs, groups, embeddings, hard filters.

Revision ID: 0005_m1_collection
Revises: 0004_m0_totp_replay

Source: docs/superpowers/specs/2026-10-16-job-hunter-design.md, staged by
docs/superpowers/plans/2026-10-17-m1-collection-pipeline.md (Task 1). The DDL below is copied verbatim from the
spec as static SQL, one `op.execute` per statement, in dependency order. Section 7.8 comes before 7.7 because
`fetch_requests` and `raw_responses` reference `quota_ledger`. Every forward and cyclic foreign key is added
afterwards with ALTER TABLE ... ADD FOREIGN KEY, as the spec does (the one of `quota_ledger.fetch_request_id`
right after `fetch_requests`):

  §7.5   sources, watchlist_companies (+ watchlist_company_key), source_instances, queries
         (+ queries_text_live), coverage_cells, coverage_intervals (+ coverage_intervals_cell, coverage_open_gaps)
  §7.8   quota_windows (+ quota_windows_current), quota_ledger (+ quota_ledger_window, quota_ledger_rolling,
         quota_ledger_unsettled)
  §7.7   fetch_requests (+ fetch_requests_run), raw_responses (+ raw_responses_expiry, raw_responses_body),
         raw_items (+ raw_items_attention, raw_items_arbeitnow_ts), ats_snapshots (+ ats_snapshots_instance),
         ats_snapshot_members
  §7.9   postings (+ postings_job, postings_active_seen), posting_observations (+ observations_posting), jobs
         (+ jobs_list_posted, jobs_list_first_seen, jobs_title_trgm, jobs_company, jobs_group, jobs_sources),
         job_revisions (+ job_revisions_tsv, job_revisions_awaiting), job_revision_occurrences,
         job_revision_sources (+ job_revision_sources_source), ad_keys (+ ad_keys_job), ad_key_sources
         (+ ad_key_sources_posting), merge_events (+ merge_events_into), identity_conflicts
         (+ identity_conflicts_open), duplicate_groups, duplicate_group_members, dedup_decisions,
         job_tombstones, tombstone_keys
  §7.10  embeddings
  §7.11  hard_filter_results
  §7.14  the foreign key purge_fences.source_id -> sources, and is_fenced with its 'source' branch
         (CREATE OR REPLACE over the M0 function)
  §7.16  one assert_transition trigger per M1 state machine: fetch_requests(status), raw_items(disposition),
         postings(availability), quota_ledger(state), duplicate_groups(status), identity_conflicts(status),
         coverage_intervals(gap_status), quota_windows(state); `migrate` writes their edges to
         transition_matrix from the code registry

These are the ONLY differences from the spec text. Each one carries a SQL comment naming its owner (plan
Deviations 1, 2 and 22, M0 Deviations 1 and 2, and plan Task 1 for the constraint names):

  1. queries.proposal_id has no foreign key: M2 adds REFERENCES query_proposals(id) ON DELETE SET NULL with
     query_proposals. query_proposals and profile_vectors are not created (M2, with profile_versions).
  2. hard_filter_results is created here, because resolve_identity and node 8 write it; the rest of §7.11
     is M3's.
  3. The source_instances breaker CHECK reads (health IN ('breaker_open','auth_failed','contract_broken',
     'not_found')) = (breaker_opened_at IS NOT NULL AND half_open_after IS NOT NULL): every open state has
     the 6-hour half-open timestamps (the spec: health = 'breaker_open' only).
  4. ats_snapshot_absences is not in the spec: the frozen absent set of a mass-disappearance opening snapshot.
  5. The CHECK constraints a test asserts by name carry an explicit CONSTRAINT name, because the spec leaves
     them unnamed and PostgreSQL's generated <table>_check<n> names depend on DDL order:
     fetch_requests_skipped_reason_check, quota_windows_tokens_check (tokens <= capacity),
     source_instances_breaker_check, coverage_intervals_covered_reason_check. The column CHECK
     (tokens >= 0) of quota_windows is named quota_windows_tokens_nonnegative_check: its generated name would
     be quota_windows_tokens_check, which PostgreSQL then refuses for the table CHECK.
  6. is_fenced has the 'global', 'jobs' and 'source' branches; M2 adds the 'profile' branch with
     llm_tuples, ending at the §7.14 body. purge_fences.manifest_id -> deletion_manifests stays M5's.

Migrations are expand/contract: there is no downgrade.
"""

# ruff: noqa: E501  (the DDL is copied from the spec; its long lines stay as they are)

from collections.abc import Sequence

from alembic import op

revision: str = "0005_m1_collection"
down_revision: str | Sequence[str] | None = "0004_m0_totp_replay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # §7.5 Sources, watchlist, queries, coverage
    op.execute("""\
CREATE TABLE sources (
  id                   text PRIMARY KEY CHECK (id IN ('jsearch','serpapi','adzuna','arbeitnow','greenhouse',
                                                      'lever','ashby','personio','recruitee','smartrecruiters')),
  kind                 text NOT NULL CHECK (kind IN ('paid_search','quota_search','firehose','ats')),
  precedence_rank      smallint NOT NULL,          -- 1 = ATS, 2 = JSearch/SerpApi, 3 = Arbeitnow, 4 = Adzuna
  can_close            boolean NOT NULL,           -- true only for ATS
  default_completeness text NOT NULL CHECK (default_completeness IN ('full','partial')),
  purge_fence_at       timestamptz                 -- record of the last source purge (§20.5); the fence is a purge_fences row
);                                                 -- seeded by migration""")
    op.execute("""\
CREATE TABLE watchlist_companies (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  display_name      text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 120),
  company_key       text NOT NULL,
  ats_type          text NOT NULL REFERENCES sources(id)
                    CHECK (ats_type IN ('greenhouse','lever','ashby','personio','recruitee','smartrecruiters')),
  board_slug        text NOT NULL CHECK (board_slug ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
  lever_region      text CHECK (lever_region IN ('global','eu')),
  input_url         text,
  detection         text NOT NULL CHECK (detection IN ('pattern','manual')),
  validation_status text NOT NULL DEFAULT 'unvalidated'
                    CHECK (validation_status IN ('unvalidated','ok','not_found','error')),
  validated_at      timestamptz,
  enabled           boolean NOT NULL DEFAULT true,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CHECK ((ats_type = 'lever') = (lever_region IS NOT NULL)),
  UNIQUE NULLS NOT DISTINCT (ats_type, board_slug, lever_region)
);""")
    op.execute("CREATE INDEX watchlist_company_key ON watchlist_companies (company_key);")
    op.execute("""\
CREATE TABLE source_instances (
  id                          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id                   text NOT NULL REFERENCES sources(id),
  instance_key                text NOT NULL,          -- 'default' or '<slug>' / '<region>:<site>'
  watchlist_company_id        bigint UNIQUE REFERENCES watchlist_companies(id) ON DELETE SET NULL,
  health                      text NOT NULL DEFAULT 'ok' CHECK (health IN
                              ('ok','degraded','breaker_open','auth_failed','contract_broken','not_found','disabled')),
  consecutive_failures        integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
  breaker_opened_at           timestamptz,
  half_open_after             timestamptz,
  last_success_at             timestamptz,
  last_complete_snapshot_id   bigint,                 -- ATS only; FK below
  last_snapshot_active_count  integer CHECK (last_snapshot_active_count >= 0),  -- mass-disappearance baseline
  mass_event_snapshot_id      bigint,                 -- first snapshot of an unconfirmed mass disappearance
  last_error_code             text,
  UNIQUE (source_id, instance_key),
  CONSTRAINT source_instances_breaker_check   -- all four open states (plan Deviation 2; the spec: health = 'breaker_open')
    CHECK ((health IN ('breaker_open','auth_failed','contract_broken','not_found')) = (breaker_opened_at IS NOT NULL AND half_open_after IS NOT NULL))
);""")
    op.execute("""\
CREATE TABLE queries (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  text         text NOT NULL CHECK (length(text) BETWEEN 2 AND 120),
  lang         text NOT NULL CHECK (lang IN ('de','en')),
  origin       text NOT NULL CHECK (origin IN ('llm_proposal','user')),
  exploration  boolean NOT NULL DEFAULT false,
  proposal_id  bigint,                                  -- M2 adds REFERENCES query_proposals(id) ON DELETE SET NULL
  created_at   timestamptz NOT NULL DEFAULT now(),
  retired_at   timestamptz                             -- text edits retire + create a new row
);""")
    op.execute("CREATE UNIQUE INDEX queries_text_live ON queries (lower(text)) WHERE retired_at IS NULL;")
    op.execute("""\
CREATE TABLE coverage_cells (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_instance_id     bigint NOT NULL REFERENCES source_instances(id),
  query_id               bigint REFERENCES queries(id),
  location_key           text,                       -- scope location slug or 'remote-de'
  enabled                boolean NOT NULL DEFAULT true,
  last_attempted_at      timestamptz,
  last_covered_at        timestamptz,                -- contiguous coverage frontier (§5.6)
  last_success_at        timestamptz,
  continuation           jsonb,                      -- JSearch cursor + obtained_at, Arbeitnow seek state
  yield_ema              real NOT NULL DEFAULT 0.5 CHECK (yield_ema >= 0),
  zero_yield_streak      integer NOT NULL DEFAULT 0,
  consecutive_incomplete integer NOT NULL DEFAULT 0,
  last_exploration_at    timestamptz,
  UNIQUE NULLS NOT DISTINCT (source_instance_id, query_id, location_key)
);""")
    op.execute("""\
CREATE TABLE coverage_intervals (                    -- metadata retained 13 months (§20.1)
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  cell_id     bigint NOT NULL REFERENCES coverage_cells(id) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (kind IN ('covered','gap')),
  span        tstzrange NOT NULL CHECK (NOT isempty(span)),   -- always [older, newer)
  gap_status  text CHECK (gap_status IN ('open','closed','abandoned')),
  gap_cursor  jsonb,
  reason      text CHECK (reason IN ('page_cap','allowance','ambiguous_attempt','unknown_continuation',
                'beyond_provider_window','incomplete_snapshot','source_failed','shutdown')),
  run_id      bigint REFERENCES runs(id) ON DELETE SET NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  closed_at   timestamptz,
  UNIQUE (cell_id, kind, span),
  CHECK ((kind = 'gap') = (gap_status IS NOT NULL)),
  CONSTRAINT coverage_intervals_covered_reason_check CHECK ((kind = 'covered') = (reason IS NULL))   -- named for its test (plan Task 1)
);""")
    op.execute("CREATE INDEX coverage_intervals_cell ON coverage_intervals (cell_id, upper(span) DESC);")
    op.execute(
        "CREATE INDEX coverage_open_gaps ON coverage_intervals (cell_id) WHERE kind = 'gap' AND gap_status = 'open';"
    )

    # §7.8 Quota ledger (before §7.7: fetch_requests and raw_responses reference quota_ledger)
    op.execute("""\
CREATE TABLE quota_windows (                         -- every window is kept (§6.3)
  provider             text NOT NULL CHECK (provider IN ('jsearch','serpapi','adzuna')),
  window_start         timestamptz NOT NULL,
  window_end           timestamptz NOT NULL,
  is_current           boolean NOT NULL,
  closed_reason        text CHECK (closed_reason IN ('rolled_over','provider_reset_observed')),
  limit_units          integer NOT NULL CHECK (limit_units >= 0),
  reserve_units        integer NOT NULL CHECK (reserve_units >= 0),
  state                text NOT NULL CHECK (state IN ('validated','extrapolated_unvalidated','exhausted_until_reset')),
  tokens               numeric(10,4) NOT NULL CONSTRAINT quota_windows_tokens_nonnegative_check CHECK (tokens >= 0),   -- named: generated, it would take quota_windows_tokens_check (plan Task 1)
  tokens_at            timestamptz NOT NULL,
  capacity             numeric(10,4) NOT NULL CHECK (capacity >= 1),
  probe_used           boolean NOT NULL DEFAULT false,
  probe_failed         boolean NOT NULL DEFAULT false,
  next_probe_at        timestamptz,
  last_observed_at     timestamptz,                  -- monotonic (§6.3 step 0)
  observed_remaining   integer,
  observed_limit       integer,
  observed_this_hour   integer,                      -- SerpApi
  observed_hourly_cap  integer,                      -- SerpApi
  conservative_until   timestamptz,                  -- SerpApi foreign-usage mode (§6.5)
  hour_observed_at     timestamptz,                  -- SerpApi
  PRIMARY KEY (provider, window_start),
  CHECK (window_end > window_start),
  CHECK (reserve_units <= limit_units),
  CONSTRAINT quota_windows_tokens_check CHECK (tokens <= capacity),   -- named for its test (plan Task 1)
  CHECK (is_current = (closed_reason IS NULL))
);""")
    op.execute("CREATE UNIQUE INDEX quota_windows_current ON quota_windows (provider) WHERE is_current;")
    op.execute("""\
CREATE TABLE quota_ledger (                          -- retained indefinitely; holds no content
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider           text NOT NULL,
  window_start       timestamptz NOT NULL,
  kind               text NOT NULL CHECK (kind IN ('request','adjustment')),
  request_class      text CHECK (request_class IN ('normal','retry','probe')),
  request_key        bytea,
  attempt_seq        smallint CHECK (attempt_seq BETWEEN 1 AND 8),
  run_id             bigint REFERENCES runs(id) ON DELETE SET NULL,
  run_attempt_no     integer,
  fetch_request_id   bigint UNIQUE,                       -- FK to fetch_requests ON DELETE SET NULL (deferrable)
  units              smallint NOT NULL CHECK (units >= 0),
  state              text CHECK (state IN ('reserved','dispatched','billed','not_billed','ambiguous')),
  evidence           text,
  http_status        smallint,
  provider_remaining integer,
  created_at         timestamptz NOT NULL DEFAULT now(),  -- = reservation time
  dispatched_at      timestamptz,
  settled_at         timestamptz,
  FOREIGN KEY (provider, window_start) REFERENCES quota_windows(provider, window_start),
  UNIQUE (provider, request_key, attempt_seq),
  CHECK ((kind = 'request') = (state IS NOT NULL AND request_key IS NOT NULL AND request_class IS NOT NULL
                               AND attempt_seq IS NOT NULL)),
  CHECK (state <> 'not_billed' OR evidence IN ('rapidapi_gateway_404','connect_failed_before_send','never_dispatched')),
  CHECK (kind <> 'adjustment' OR (units > 0 AND evidence IS NOT NULL)),
  CHECK (state NOT IN ('billed','not_billed','ambiguous') OR settled_at IS NOT NULL),
  CHECK (state <> 'reserved' OR dispatched_at IS NULL)
);""")
    op.execute("CREATE INDEX quota_ledger_window ON quota_ledger (provider, window_start, state);")
    op.execute("CREATE INDEX quota_ledger_rolling ON quota_ledger (provider, created_at DESC);")
    op.execute(
        "CREATE INDEX quota_ledger_unsettled ON quota_ledger (state, created_at) WHERE state IN ('reserved','dispatched');"
    )

    # §7.7 Acquisition receipts
    op.execute("""\
CREATE TABLE fetch_requests (                        -- metadata retained 13 months
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id                  bigint NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  run_attempt_no          integer NOT NULL,
  cell_id                 bigint NOT NULL REFERENCES coverage_cells(id) ON DELETE CASCADE,
  source_instance_id      bigint NOT NULL REFERENCES source_instances(id),
  phase                   text NOT NULL CHECK (phase IN ('primary','fallback')),
  operation               text NOT NULL CHECK (operation IN ('list','detail')),
  detail_native_id        text,
  parent_fetch_request_id bigint REFERENCES fetch_requests(id) ON DELETE CASCADE,
  request_key             bytea NOT NULL,                       -- canonical request hash (§5.1)
  attempt_seq             smallint NOT NULL DEFAULT 1 CHECK (attempt_seq BETWEEN 1 AND 8),
  page_no                 smallint NOT NULL CHECK (page_no >= 1),
  request_class           text NOT NULL CHECK (request_class IN ('normal','retry','probe','free')),
  spec                    jsonb NOT NULL,                       -- never contains secrets
  status                  text NOT NULL CHECK (status IN ('planned','reserved','dispatched','succeeded',
                                                          'failed','ambiguous','skipped')),
  skip_reason             text CHECK (skip_reason IN ('paced','reserve_held','exhausted','window_cap',
                            'breaker_open','awaiting_window_validation','shutdown','run_cancelled',
                            'superseded','not_needed')),
  error_code              text,
  http_status             smallint,
  quota_ledger_id         bigint UNIQUE REFERENCES quota_ledger(id),
  raw_response_id         bigint UNIQUE,                        -- FK below
  continuation            text CHECK (continuation IN ('cursor','end','unknown')),
  next_cursor             jsonb,
  items                   integer CHECK (items >= 0),
  new_native_ids          integer CHECK (new_native_ids >= 0), -- peek_native_ids vs postings (§5.3.1)
  new_postings            integer CHECK (new_postings >= 0),
  created_at              timestamptz NOT NULL DEFAULT now(),
  dispatched_at           timestamptz,
  finished_at             timestamptz,
  UNIQUE (request_key, attempt_seq),
  CONSTRAINT fetch_requests_skipped_reason_check CHECK ((status = 'skipped') = (skip_reason IS NOT NULL)),   -- named for its test (plan Task 1)
  CHECK (status <> 'succeeded' OR raw_response_id IS NOT NULL),
  CHECK (status <> 'failed' OR error_code IS NOT NULL),
  CHECK ((operation = 'detail') = (detail_native_id IS NOT NULL AND parent_fetch_request_id IS NOT NULL)),
  CHECK (request_class = 'free' OR status IN ('planned','skipped') OR quota_ledger_id IS NOT NULL)
);""")
    op.execute("CREATE INDEX fetch_requests_run ON fetch_requests (run_id, status);")

    # §7.8: the cyclic FK of quota_ledger, now that fetch_requests exists
    op.execute("""\
ALTER TABLE quota_ledger ADD FOREIGN KEY (fetch_request_id) REFERENCES fetch_requests(id)
  ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;""")
    op.execute("""\
CREATE TABLE raw_responses (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fetch_request_id   bigint NOT NULL UNIQUE REFERENCES fetch_requests(id) ON DELETE CASCADE,   -- row kept with it
  ledger_entry_id    bigint UNIQUE REFERENCES quota_ledger(id),    -- quota providers only
  source_instance_id bigint NOT NULL REFERENCES source_instances(id),
  http_status        smallint NOT NULL,
  headers            jsonb NOT NULL,                       -- allowlisted headers only (§5.2)
  body_zstd          bytea,                                -- full body (2xx) or first 4 KiB (errors)
  body_sha256        bytea NOT NULL,
  body_ref_id        bigint REFERENCES raw_responses(id) ON DELETE SET NULL,  -- identical body stored earlier
  item_count         integer CHECK (item_count >= 0),
  parsed_at          timestamptz,
  fetched_at         timestamptz NOT NULL,                 -- local receive time = sighting time (§8.6)
  expires_at         timestamptz NOT NULL,                 -- fetched_at + 30 d (body only; the row lives with its fetch request)
  purged_at          timestamptz,
  CHECK (purged_at IS NOT NULL OR body_zstd IS NOT NULL OR body_ref_id IS NOT NULL)
);""")
    op.execute(
        "ALTER TABLE fetch_requests ADD FOREIGN KEY (raw_response_id) REFERENCES raw_responses(id) ON DELETE SET NULL;"
    )
    op.execute("CREATE INDEX raw_responses_expiry ON raw_responses (expires_at) WHERE purged_at IS NULL;")
    op.execute("CREATE INDEX raw_responses_body ON raw_responses (body_sha256) WHERE purged_at IS NULL;")
    op.execute("""\
CREATE TABLE raw_items (                              -- retained 90 days (§20.1)
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  raw_response_id bigint NOT NULL REFERENCES raw_responses(id) ON DELETE CASCADE,
  item_index      integer NOT NULL CHECK (item_index >= 0),
  native_id       text,
  item_ts         timestamptz,                          -- the source's own item timestamp (§5.1)
  item_sha256     bytea NOT NULL,
  disposition     text NOT NULL CHECK (disposition IN ('pending','linked','out_of_scope',
                                                       'duplicate_in_page','rejected','deferred')),
                                                      -- deferred: 'job_fenced' only (§7.16.1)
  reason_code     text,
  normalized      jsonb,                                -- the parsed NormalizedPosting while pending or deferred (§9.2)
  observation_id  bigint UNIQUE,                        -- FK below (deferrable)
  created_at      timestamptz NOT NULL DEFAULT now(),
  disposed_at     timestamptz,
  UNIQUE (raw_response_id, item_index),
  CHECK ((disposition IN ('out_of_scope','duplicate_in_page','rejected','deferred')) = (reason_code IS NOT NULL)),
  CHECK (disposition <> 'deferred' OR reason_code = 'job_fenced'),
  CHECK (observation_id IS NULL OR disposition = 'linked'),   -- a purge may detach a linked item's observation
  CHECK ((disposition = 'pending') = (disposed_at IS NULL)),
  CHECK (disposition NOT IN ('pending','deferred') OR normalized IS NOT NULL),
  CHECK (disposition IN ('pending','deferred') OR normalized IS NULL)   -- dropped on final disposal
);""")
    op.execute(
        "CREATE INDEX raw_items_attention ON raw_items (disposition) WHERE disposition IN ('pending','rejected','deferred');"
    )
    op.execute("CREATE INDEX raw_items_arbeitnow_ts ON raw_items (item_ts) WHERE item_ts IS NOT NULL;")
    op.execute("""\
CREATE TABLE ats_snapshots (
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_instance_id      bigint NOT NULL REFERENCES source_instances(id) ON DELETE CASCADE,
  run_id                  bigint REFERENCES runs(id) ON DELETE SET NULL,
  status                  text NOT NULL CHECK (status IN ('complete','incomplete','mass_disappearance')),
                          -- complete and mass_disappearance are both structurally complete (§8.11)
  event_snapshot_id       bigint REFERENCES ats_snapshots(id) ON DELETE SET NULL,   -- first snapshot of the open event
  incomplete_reason       text,                         -- e.g. non_2xx, envelope, no_end, count_mismatch, missing_id
  member_count            integer NOT NULL CHECK (member_count >= 0),
  baseline_active_count   integer,                      -- source_instances.last_snapshot_active_count before it
  absent_count            integer,                      -- previously active postings absent from it
  taken_at                timestamptz NOT NULL,         -- fetched_at of its first page
  applied_at              timestamptz,                  -- availability effects applied (idempotency)
  acknowledged_at         timestamptz,                  -- user acknowledged a mass disappearance (§8.11)
  CHECK ((status = 'incomplete') = (incomplete_reason IS NOT NULL)),
  CHECK (event_snapshot_id IS NULL OR status = 'mass_disappearance')
);""")
    op.execute("CREATE INDEX ats_snapshots_instance ON ats_snapshots (source_instance_id, taken_at DESC);")
    op.execute(
        "ALTER TABLE source_instances ADD FOREIGN KEY (last_complete_snapshot_id) REFERENCES ats_snapshots(id) ON DELETE SET NULL;"
    )
    op.execute(
        "ALTER TABLE source_instances ADD FOREIGN KEY (mass_event_snapshot_id) REFERENCES ats_snapshots(id) ON DELETE SET NULL;"
    )
    op.execute("""\
CREATE TABLE ats_snapshot_members (                  -- membership is independent of normalisation
  snapshot_id bigint NOT NULL REFERENCES ats_snapshots(id) ON DELETE CASCADE,
  native_id   text NOT NULL,
  PRIMARY KEY (snapshot_id, native_id)
);""")

    # §7.9 Postings, jobs, revisions, identities, groups
    op.execute("""\
CREATE TABLE postings (
  id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_instance_id       bigint NOT NULL REFERENCES source_instances(id),
  native_id                text NOT NULL CHECK (length(native_id) BETWEEN 1 AND 256),
  job_id                   bigint NOT NULL,                  -- FK below (deferrable)
  first_seen_at            timestamptz NOT NULL,
  last_seen_at             timestamptz NOT NULL,             -- monotonic: greatest(sighting times)
  seen_count               integer NOT NULL DEFAULT 1 CHECK (seen_count >= 1),
  availability             text NOT NULL CHECK (availability IN ('active','stale','closed','unknown')),
  availability_changed_at  timestamptz NOT NULL,
  availability_reason      text,                             -- e.g. ats_absent, mass_disappearance_confirmed
  absent_snapshots         smallint NOT NULL DEFAULT 0 CHECK (absent_snapshots BETWEEN 0 AND 2),
  first_absent_at          timestamptz,
  last_absent_snapshot_id  bigint REFERENCES ats_snapshots(id) ON DELETE SET NULL,
  latest_observation_id    bigint,                           -- FK below; monotonic by observed_at
  outbound_url_private     text,                             -- Adzuna redirect_url only
  list_item_sha256         bytea,                            -- SmartRecruiters change detection
  detail_fetched_at        timestamptz,
  UNIQUE (source_instance_id, native_id),
  CHECK (last_seen_at >= first_seen_at),
  CHECK ((absent_snapshots = 0) = (first_absent_at IS NULL))
);""")
    op.execute("CREATE INDEX postings_job ON postings (job_id);")
    op.execute("CREATE INDEX postings_active_seen ON postings (last_seen_at) WHERE availability = 'active';")
    op.execute("""\
CREATE TABLE ats_snapshot_absences (              -- plan Deviation 22: the frozen absent set of a mass-disappearance opening snapshot
  snapshot_id bigint NOT NULL REFERENCES ats_snapshots(id) ON DELETE CASCADE,
  posting_id  bigint NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
  PRIMARY KEY (snapshot_id, posting_id)
);""")
    op.execute("""\
CREATE TABLE posting_observations (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  posting_id         bigint NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
  raw_item_id        bigint UNIQUE,                          -- FK below, ON DELETE SET NULL
  source_instance_id bigint NOT NULL REFERENCES source_instances(id),   -- provenance copy
  raw_response_sha256 bytea NOT NULL,                        -- provenance copy
  observed_at        timestamptz NOT NULL,                   -- = raw_responses.fetched_at
  normalized         jsonb NOT NULL,                         -- NormalizedPosting
  content_hash       bytea NOT NULL,
  completeness       text NOT NULL CHECK (completeness IN ('full','partial')),
  quality            text NOT NULL CHECK (quality IN ('ok','short','garbage'))
);""")
    op.execute("CREATE INDEX observations_posting ON posting_observations (posting_id, observed_at DESC);")
    op.execute("""\
ALTER TABLE posting_observations ADD FOREIGN KEY (raw_item_id) REFERENCES raw_items(id)
  ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;""")
    op.execute("""\
ALTER TABLE raw_items ADD FOREIGN KEY (observation_id) REFERENCES posting_observations(id)
  ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;""")
    op.execute(
        "ALTER TABLE postings ADD FOREIGN KEY (latest_observation_id) REFERENCES posting_observations(id);"
    )
    op.execute("""\
CREATE TABLE jobs (
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  merged_into_job_id      bigint REFERENCES jobs(id),
  current_revision_id     bigint,                            -- FK below, DEFERRABLE
  company_key             text NOT NULL,
  company_display         text NOT NULL,
  title                   text NOT NULL,
  title_norm              text NOT NULL,
  seniority_tokens        text[] NOT NULL DEFAULT '{}',
  locations               jsonb NOT NULL DEFAULT '[]',       -- [{label, resolution, candidates[], postal_code, source}]
  location_kind           text NOT NULL CHECK (location_kind IN ('city','multi','country_wide','remote','unknown')),
  work_mode_possible      text[] NOT NULL
                          CHECK (cardinality(work_mode_possible) >= 1
                                 AND work_mode_possible <@ ARRAY['remote','hybrid','onsite']),
  work_mode_source        text NOT NULL CHECK (work_mode_source IN ('structured','text','none')),
  remote_countries        text[] NOT NULL DEFAULT '{}',      -- structured ISO codes only (§5.1)
  employment_types        text[] NOT NULL DEFAULT '{}',
  employment_source       text NOT NULL CHECK (employment_source IN ('structured','text','none')),
  salary                  jsonb,                             -- §8.10
  industry_structured     text,
  ad_language             text NOT NULL CHECK (ad_language IN ('de','en','other','unknown')),
  completeness            text NOT NULL CHECK (completeness IN ('full','partial')),
  posted_at_effective     timestamptz,
  posted_precision        text NOT NULL CHECK (posted_precision IN ('exact','hour','day','relative_day','ingest','unknown')),
  first_seen_at           timestamptz NOT NULL,
  last_seen_at            timestamptz NOT NULL,
  availability            text NOT NULL CHECK (availability IN ('active','stale','closed','unknown')),
  availability_changed_at timestamptz NOT NULL,              -- drives retention (§20.1)
  source_ids              text[] NOT NULL,
  duplicate_group_id      bigint,                            -- FK below
  field_provenance        jsonb NOT NULL,                    -- {field: {source_instance_id, posting_id,
                                                             --  observation_id, observed_at, rule}}
  det_facts_hash          bytea NOT NULL,                    -- hash of fields used by det_results/filters
  restored_from_tombstone_id bigint,                         -- §8.6 step 2
  updated_at              timestamptz NOT NULL DEFAULT now(),
  CHECK (merged_into_job_id IS NULL OR merged_into_job_id <> id),
  CHECK (posted_at_effective IS NULL OR posted_at_effective <= first_seen_at),
  CHECK ((posted_precision = 'unknown') = (posted_at_effective IS NULL))
);""")
    op.execute("""\
CREATE INDEX jobs_list_posted ON jobs (availability, posted_at_effective DESC NULLS LAST, id DESC)
  WHERE merged_into_job_id IS NULL;""")
    op.execute(
        "CREATE INDEX jobs_list_first_seen ON jobs (first_seen_at DESC, id DESC) WHERE merged_into_job_id IS NULL;"
    )
    op.execute("CREATE INDEX jobs_title_trgm ON jobs USING gin (title_norm gin_trgm_ops);")
    op.execute("CREATE INDEX jobs_company ON jobs (company_key);")
    op.execute("CREATE INDEX jobs_group ON jobs (duplicate_group_id) WHERE duplicate_group_id IS NOT NULL;")
    op.execute("CREATE INDEX jobs_sources ON jobs USING gin (source_ids);")
    op.execute("""\
CREATE TABLE job_revisions (
  id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id                   bigint NOT NULL REFERENCES jobs(id),
  revision_no              integer NOT NULL CHECK (revision_no >= 1),
  material_hash            bytea NOT NULL,                     -- sha256 of llm_input (canonical JSON)
  llm_input                jsonb NOT NULL,                     -- the projection rendered into prompts (§8.8)
  description_text         text NOT NULL,                      -- normalised; evidence offsets refer to it
  description_completeness text NOT NULL CHECK (description_completeness IN ('full','partial')),
  ad_language              text NOT NULL CHECK (ad_language IN ('de','en','other','unknown')),
  embedding_status         text NOT NULL CHECK (embedding_status IN ('awaiting_embedding','embedded')),
  embedding_id             bigint,                             -- FK below
  embedding_truncated      boolean NOT NULL DEFAULT false,
  tsv                      tsvector GENERATED ALWAYS AS (
                             CASE WHEN ad_language = 'en'
                               THEN setweight(to_tsvector('english', coalesce(llm_input->>'title','')), 'A') ||
                                    setweight(to_tsvector('english', left(description_text, 20000)), 'C')
                               ELSE setweight(to_tsvector('german_unaccent', coalesce(llm_input->>'title','')), 'A') ||
                                    setweight(to_tsvector('german_unaccent', left(description_text, 20000)), 'C')
                             END) STORED,
  created_at               timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, revision_no),
  UNIQUE (job_id, material_hash),                              -- an identical projection reuses the revision
  CHECK ((embedding_status = 'embedded') = (embedding_id IS NOT NULL))
);""")
    op.execute("CREATE INDEX job_revisions_tsv ON job_revisions USING gin (tsv);")
    op.execute(
        "CREATE INDEX job_revisions_awaiting ON job_revisions (created_at) WHERE embedding_status = 'awaiting_embedding';"
    )
    op.execute(
        "ALTER TABLE jobs ADD FOREIGN KEY (current_revision_id) REFERENCES job_revisions(id) DEFERRABLE INITIALLY DEFERRED;"
    )
    op.execute(
        "ALTER TABLE postings ADD FOREIGN KEY (job_id) REFERENCES jobs(id) DEFERRABLE INITIALLY DEFERRED;"
    )
    op.execute("""\
CREATE TABLE job_revision_occurrences (               -- A → B → A history (§8.8)
  job_id            bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  job_revision_id   bigint NOT NULL REFERENCES job_revisions(id) ON DELETE CASCADE,
  became_current_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, became_current_at)
);""")
    op.execute("""\
CREATE TABLE job_revision_sources (                   -- immutable lineage of every revision (§20.5)
  job_revision_id bigint NOT NULL REFERENCES job_revisions(id) ON DELETE CASCADE,
  field           text NOT NULL,                       -- a llm_input field, e.g. description_text, title
  source_id       text NOT NULL REFERENCES sources(id),
  posting_id      bigint,                              -- no FK: lineage survives posting deletion
  observation_id  bigint,
  PRIMARY KEY (job_revision_id, field, source_id)
);""")
    op.execute("CREATE INDEX job_revision_sources_source ON job_revision_sources (source_id);")
    op.execute("""\
CREATE TABLE ad_keys (                                -- resolved ownership: one job per key
  key_ns      text NOT NULL,      -- 'htidocid' | 'pub:<host>' | 'adzuna' | 'arbeitnow' | 'ats:<type>:<board>'
  key_value   text NOT NULL,
  exclusive   boolean NOT NULL,   -- one value per job per namespace (all except 'htidocid')
  job_id      bigint NOT NULL REFERENCES jobs(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (key_ns, key_value)
);""")
    op.execute("CREATE INDEX ad_keys_job ON ad_keys (job_id);")
    op.execute("""\
CREATE TABLE ad_key_sources (                         -- provenance: every posting that supplied a key
  key_ns         text NOT NULL,
  key_value      text NOT NULL,
  posting_id     bigint NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
  observation_id bigint REFERENCES posting_observations(id) ON DELETE SET NULL,
  first_seen_at  timestamptz NOT NULL,
  PRIMARY KEY (key_ns, key_value, posting_id),
  FOREIGN KEY (key_ns, key_value) REFERENCES ad_keys(key_ns, key_value) ON DELETE CASCADE
);""")
    op.execute("CREATE INDEX ad_key_sources_posting ON ad_key_sources (posting_id);")
    op.execute("""\
CREATE TABLE merge_events (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind                 text NOT NULL CHECK (kind IN ('strong_key_merge','user_confirmed_merge','unmerge')),
  from_job_id          bigint NOT NULL REFERENCES jobs(id),
  into_job_id          bigint NOT NULL REFERENCES jobs(id),
  posting_ids          bigint[] NOT NULL,
  evidence             jsonb NOT NULL,       -- matching keys, rule, observation ids, and the pre-merge
                                             -- snapshot: user_job_actions of both jobs, group memberships,
                                             -- key ownership rows moved, targets cancelled
  created_at           timestamptz NOT NULL DEFAULT now(),
  reversed_by_event_id bigint REFERENCES merge_events(id),
  CHECK (from_job_id <> into_job_id)
);""")
    op.execute("CREATE INDEX merge_events_into ON merge_events (into_job_id);")
    op.execute("""\
CREATE TABLE identity_conflicts (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  posting_id        bigint NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
  posting_job_id    bigint NOT NULL REFERENCES jobs(id),             -- the job the posting stays on
  candidate_job_ids bigint[] NOT NULL CHECK (cardinality(candidate_job_ids) >= 1),  -- the other jobs
  evidence          jsonb NOT NULL,         -- conflicting exclusive keys
  status            text NOT NULL CHECK (status IN ('open','resolved_separate','resolved_merged')),
  created_at        timestamptz NOT NULL DEFAULT now(),
  resolved_at       timestamptz,
  CHECK ((status = 'open') = (resolved_at IS NULL)),
  CHECK (NOT (posting_job_id = ANY (candidate_job_ids)))
);""")
    op.execute(
        "CREATE INDEX identity_conflicts_open ON identity_conflicts (created_at) WHERE status = 'open';"
    )
    op.execute("""\
CREATE TABLE duplicate_groups (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  representative_job_id bigint REFERENCES jobs(id),
  status                text NOT NULL CHECK (status IN ('active','dissolved')),
  unscorable_reason     text CHECK (unscorable_reason IN ('all_failed','all_filtered','no_active_member')),
  elected_at            timestamptz NOT NULL DEFAULT now(),
  election_reason       text NOT NULL,      -- initial | rep_inactive | rep_ineligible | fuller_member_joined |
                                            -- split | member_left | reconciler_repair | eligible_again
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (status = 'dissolved' OR (representative_job_id IS NULL) = (unscorable_reason IS NOT NULL))
);""")
    op.execute(
        "ALTER TABLE jobs ADD FOREIGN KEY (duplicate_group_id) REFERENCES duplicate_groups(id) ON DELETE SET NULL;"
    )
    op.execute("""\
CREATE TABLE duplicate_group_members (
  group_id           bigint NOT NULL REFERENCES duplicate_groups(id) ON DELETE CASCADE,
  job_id             bigint NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,   -- ≤ 1 group per job
  joined_at          timestamptz NOT NULL DEFAULT now(),
  evidence           jsonb NOT NULL,                      -- trigram, shingle scores, location rule
  score_individually boolean NOT NULL DEFAULT false,      -- user intent; survives new revisions (§8.7)
  PRIMARY KEY (group_id, job_id)
);""")
    op.execute("""\
CREATE TABLE dedup_decisions (
  job_a      bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  job_b      bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  decision   text NOT NULL CHECK (decision IN ('different','same')),
  source     text NOT NULL CHECK (source IN ('user_split','user_confirm','unmerge','conflict_resolution')),
  decided_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_a, job_b),
  CHECK (job_a < job_b)
);""")
    op.execute("""\
CREATE TABLE job_tombstones (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  former_job_id bigint NOT NULL UNIQUE,
  user_actions  jsonb,                     -- timestamps only (seen/saved/applied/hidden), never notes
  tombstoned_at timestamptz NOT NULL DEFAULT now()
);""")
    op.execute("""\
CREATE TABLE tombstone_keys (
  key_hash     bytea PRIMARY KEY,          -- sha256(key_ns ‖ key_value)
  tombstone_id bigint NOT NULL REFERENCES job_tombstones(id) ON DELETE CASCADE
);""")
    op.execute(
        "ALTER TABLE jobs ADD FOREIGN KEY (restored_from_tombstone_id) REFERENCES job_tombstones(id) ON DELETE SET NULL;"
    )

    # §7.10 Embeddings (profile_vectors is created by M2 with profile_versions: plan Deviation 1)
    op.execute("""\
CREATE TABLE embeddings (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  embedding_model_id text NOT NULL,          -- e.g. 'snowflake-arctic-embed-l-v2.0@<rev>/int8'
  text_sha256        bytea NOT NULL,
  input_kind         text NOT NULL CHECK (input_kind IN ('query','passage')),
  vector             halfvec(1024) NOT NULL, -- L2-normalised
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (embedding_model_id, text_sha256, input_kind)
);
-- Exact scan until > 50,000 rows; then (only after measurement):
-- CREATE INDEX embeddings_hnsw ON embeddings USING hnsw (vector halfvec_cosine_ops);""")
    op.execute("ALTER TABLE job_revisions ADD FOREIGN KEY (embedding_id) REFERENCES embeddings(id);")

    # §7.11 Scoring: hard_filter_results only (plan Deviation 1: resolve_identity and node 8 write it; the rest of §7.11 is M3's)
    op.execute("""\
CREATE TABLE hard_filter_results (                   -- current projection per (job, scope)
  job_id                bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  scope_version_id      bigint NOT NULL REFERENCES scope_versions(id),
  passed                boolean NOT NULL,
  reasons               text[] NOT NULL DEFAULT '{}',    -- §11.1 filter codes
  inputs_hash           bytea NOT NULL,                  -- jobs.det_facts_hash at compute time
  det_algorithm_version text NOT NULL,                   -- const DET_ALGORITHM_VERSION (§11.11)
  computed_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, scope_version_id),
  CHECK (passed = (cardinality(reasons) = 0))
);""")

    # §7.14 The purge fence of a source
    op.execute(
        "ALTER TABLE purge_fences ADD FOREIGN KEY (source_id) REFERENCES sources(id);   -- the FK M0 left out (M0 Deviation 1)"
    )
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
            -- M2 adds the 'profile' branch with llm_tuples (M0 Deviation 2): p_tuple is unused until then
           ));
$$;                                                -- callers hold the job row FOR SHARE (§20.2)""")

    # §7.16 The defence-in-depth trigger of every M1 state machine (the edges: seed.sync_transition_matrix)
    op.execute(
        "CREATE TRIGGER fetch_requests_assert_transition BEFORE UPDATE OF status ON fetch_requests "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('status');"
    )
    op.execute(
        "CREATE TRIGGER raw_items_assert_transition BEFORE UPDATE OF disposition ON raw_items "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('disposition');"
    )
    op.execute(
        "CREATE TRIGGER postings_assert_transition BEFORE UPDATE OF availability ON postings "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('availability');"
    )
    op.execute(
        "CREATE TRIGGER quota_ledger_assert_transition BEFORE UPDATE OF state ON quota_ledger "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('state');"
    )
    op.execute(
        "CREATE TRIGGER duplicate_groups_assert_transition BEFORE UPDATE OF status ON duplicate_groups "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('status');"
    )
    op.execute(
        "CREATE TRIGGER identity_conflicts_assert_transition BEFORE UPDATE OF status ON identity_conflicts "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('status');"
    )
    op.execute(
        "CREATE TRIGGER coverage_intervals_assert_transition BEFORE UPDATE OF gap_status ON coverage_intervals "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('gap_status');"
    )
    op.execute(
        "CREATE TRIGGER quota_windows_assert_transition BEFORE UPDATE OF state ON quota_windows "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('state');"
    )


def downgrade() -> None:
    raise NotImplementedError("expand/contract: no downgrades")
