"""M4 schema: the list indexes of the unscored section and of the posted sort, the index of the merge events
from a job, the indexes of the header status's freshness and scope reads, the radius functions, and the
`replay_raw_items` operation kind.

Revision ID: 0009_m4_ui
Revises: 0008_m3_scoring

Staged by docs/superpowers/plans/2026-11-02-m4-api-and-web-ui.md (Task 2; Task 6 added T6, Task 11 the two
T11 indexes, Ruling T11-d), as static SQL, one `op.execute` per statement:

  B1  score_targets_unscored_rank: the unscored section of the Jobs list (§14.3) walks a tuple's targets,
      but for the published, the deferred duplicates and the cancelled, by similarity (NULLS LAST), then job
      id, both descending;
  B6  jobs_list_posted_any: the `posted` sort over every availability. M1's jobs_list_posted leads with
      `availability`, so it serves only a list filtered to one availability;
  T6  merge_events_from_job: the drawer's merge events from a job (Task 6, Ruling T6-e). M1 indexes only
      `into_job_id` (merge_events_into), so the events a job was merged in by would scan the table;
  T11 runs_last_success: the latest `finished_at` of a `succeeded` or `partial` run, and of a `succeeded` one
      (`status` included, so that maximum is index-only too), which the freshness rule reads
      (`observability.health.read_run_freshness`: the header status every 60 s, the system health and the
      `stale_data` reconciler). Without it both maxima scan `runs`;
  T11 operations_dedup_newest: the newest operation of a dedup key, by id, whatever its status: the newest
      ended apply of a scope (`settings.scope_apply.scope_applied`, every header status poll) and the newest
      apply of the pending scope (its `scope_apply_failed` reconciler). `operations_live_dedup` covers the
      live operations only, so both would scan `operations`;
  B5  geo_km(lat1, lon1, lat2, lon2): the haversine distance in km on the sphere of radius 6371.0088 km, the
      formula of `geo.gazetteer.distance_km` term by term, `asin(least(1, sqrt(a)))` included; and
      job_within_km(locations, lat, lon, radius_km): true when a candidate of a `unique` or `ambiguous` entry
      of a job's `locations` (`sources.parsing.locations.Location`) lies within `radius_km` of the point. An
      `unresolved` entry never matches, whatever it holds. A candidate passes a bounding-box pre-check before
      `geo_km` decides: |Δlat| ≤ radius/110.5, and, unless the circle may reach a pole (|lat| + radius/110.5
      ≥ 89, where no longitude bound holds), the shorter way round the meridians, least(|Δlon|, 360 -
      |Δlon|), at most radius / (110.5 · cos(radians(|lat| + radius/110.5))). The box is a superset of the
      circle everywhere, poles and antimeridian included (Ruling T2-a): a degree of latitude is 111.2 km on
      this sphere, more than 110.5, and no point within the circle lies poleward of |lat| + radius/110.5,
      where a degree of longitude is shortest, so it only spares `geo_km` the candidates far away. Both are
      IMMUTABLE STRICT PARALLEL SAFE; their bodies are SQL strings, so PostgreSQL records no dependency of
      job_within_km on geo_km and the downgrade drops them in either order;
  T12 operations.kind: the CHECK of 0001 dropped and added again with every 0001 value plus
      'replay_raw_items' (Task 12's operation). The kinds are 0001's literal, copied from
      0001_m0_baseline.py, so the downgrade restores the CHECK as 0001 wrote it.

No view changes and no INSERT. Unlike the earlier migrations, 0009 has an exact downgrade: it drops the
functions and the five indexes and restores 0001's CHECK. A database that holds a `replay_raw_items`
operation refuses that CHECK (23514) and stays at 0009: the downgrade never deletes an operation.
"""

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0009_m4_ui"
down_revision: str | Sequence[str] | None = "0008_m3_scoring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values of 0001's operations.kind CHECK, as 0001_m0_baseline.py:228-230 writes them.
KINDS_0001_SQL: Final = """'extract_profile_text','extract_profile','embed_profile',
                    'propose_queries','apply_scope','create_targets_for_tuple','batch_submit','export',
                    'purge','eval_run','watchlist_validate'"""


def upgrade() -> None:
    op.execute("""\
-- B1: the unscored section by similarity, then job id: not the published, deferred or cancelled targets
CREATE INDEX score_targets_unscored_rank
  ON score_targets (llm_tuple_id, similarity DESC NULLS LAST, job_id DESC)
  WHERE state NOT IN ('published_accepted','published_contested','deferred_duplicate','cancelled');""")
    op.execute("""\
-- B6: the posted sort over every availability
CREATE INDEX jobs_list_posted_any ON jobs (posted_at_effective DESC NULLS LAST, id DESC)
  WHERE merged_into_job_id IS NULL;""")
    op.execute("""\
-- T6: the merge events from a job (the drawer's; merge_events_into serves those into it)
CREATE INDEX merge_events_from_job ON merge_events (from_job_id);""")
    op.execute("""\
-- T11: the freshness rule's latest successful finishes; `status` included: the `succeeded` max is index-only
CREATE INDEX runs_last_success ON runs (finished_at) INCLUDE (status)
  WHERE status IN ('succeeded','partial');""")
    op.execute("""\
-- T11: the newest operation of a dedup key, any status (operations_live_dedup has the live ones only)
CREATE INDEX operations_dedup_newest ON operations (kind, dedup_key, id);""")
    op.execute("""\
-- B5: geo.gazetteer.distance_km, the haversine on the sphere of radius 6371.0088 km
CREATE FUNCTION geo_km(lat1 float8, lon1 float8, lat2 float8, lon2 float8) RETURNS float8
  LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $$
  SELECT 2 * 6371.0088::float8 * asin(least(1, sqrt(
    power(sin((radians(lat2) - radians(lat1)) / 2), 2)
    + cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lon2 - lon1) / 2), 2))))
$$;""")
    op.execute("""\
-- B5: some candidate of a resolved (unique or ambiguous) entry lies within radius_km by geo_km, after a
-- bounding box that contains the circle (no longitude bound near a pole; the shorter way across ±180°)
CREATE FUNCTION job_within_km(locations jsonb, lat float8, lon float8, radius_km float8) RETURNS boolean
  LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM jsonb_array_elements(locations) AS l,
         jsonb_array_elements(l -> 'candidates') AS c,
         LATERAL (SELECT CAST(c ->> 'lat' AS float8) AS c_lat, CAST(c ->> 'lon' AS float8) AS c_lon) AS p
    WHERE l ->> 'resolution' IN ('unique', 'ambiguous')
      AND abs(p.c_lat - lat) <= radius_km / 110.5
      AND (abs(lat) + radius_km / 110.5 >= 89
           OR least(abs(p.c_lon - lon), 360 - abs(p.c_lon - lon))
              <= radius_km / (110.5 * cos(radians(abs(lat) + radius_km / 110.5))))
      AND geo_km(lat, lon, p.c_lat, p.c_lon) <= radius_km)
$$;""")
    op.execute("ALTER TABLE operations DROP CONSTRAINT operations_kind_check;")
    op.execute(f"""\
-- Task 12: the replay of stored raw items, after 0001's kinds
ALTER TABLE operations ADD CONSTRAINT operations_kind_check CHECK (kind IN ({KINDS_0001_SQL},
                    'replay_raw_items'));""")


def downgrade() -> None:
    op.execute("ALTER TABLE operations DROP CONSTRAINT operations_kind_check;")
    op.execute(
        f"ALTER TABLE operations ADD CONSTRAINT operations_kind_check CHECK (kind IN ({KINDS_0001_SQL}));"
    )
    op.execute("DROP FUNCTION job_within_km(jsonb, float8, float8, float8);")
    op.execute("DROP FUNCTION geo_km(float8, float8, float8, float8);")
    op.execute("DROP INDEX operations_dedup_newest;")
    op.execute("DROP INDEX runs_last_success;")
    op.execute("DROP INDEX merge_events_from_job;")
    op.execute("DROP INDEX jobs_list_posted_any;")
    op.execute("DROP INDEX score_targets_unscored_rank;")
