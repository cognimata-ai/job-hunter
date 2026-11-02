"""M4 schema: the list indexes of the unscored section and of the posted sort, the radius functions, and the
`replay_raw_items` operation kind.

Revision ID: 0009_m4_ui
Revises: 0008_m3_scoring

Staged by docs/superpowers/plans/2026-11-02-m4-api-and-web-ui.md (Task 2), as static SQL, one `op.execute`
per statement:

  B1  score_targets_unscored_rank: the unscored section of the Jobs list (§14.3) walks a tuple's targets,
      but for the published, the deferred duplicates and the cancelled, by similarity (NULLS LAST), then job
      id, both descending;
  B6  jobs_list_posted_any: the `posted` sort over every availability. M1's jobs_list_posted leads with
      `availability`, so it serves only a list filtered to one availability;
  B5  geo_km(lat1, lon1, lat2, lon2): the haversine distance in km on the sphere of radius 6371.0088 km, the
      formula of `geo.gazetteer.distance_km` term by term, `asin(least(1, sqrt(a)))` included; and
      job_within_km(locations, lat, lon, radius_km): true when a candidate of a `unique` or `ambiguous` entry
      of a job's `locations` (`sources.parsing.locations.Location`) lies within `radius_km` of the point. An
      `unresolved` entry never matches, whatever it holds. Each candidate passes a bounding box first: |Δlat|
      ≤ radius/110.5 and |Δlon| ≤ radius / (110.5 · cos(radians(least(89, |lat| + radius/110.5)))), wider
      than the circle because a degree of latitude is 111.2 km on this sphere and the longitude bound takes
      the circle's latitude nearest a pole. Both are IMMUTABLE STRICT PARALLEL SAFE; their bodies are SQL
      strings, so PostgreSQL records no dependency of job_within_km on geo_km and the downgrade drops them
      in either order;
  T12 operations.kind: the CHECK of 0001 dropped and added again with every 0001 value plus
      'replay_raw_items' (Task 12's operation). The kinds are 0001's literal, copied from
      0001_m0_baseline.py, so the downgrade restores the CHECK as 0001 wrote it.

No view changes and no INSERT. Unlike the earlier migrations, 0009 has an exact downgrade: it drops the
functions and the indexes and restores 0001's CHECK. A database that holds a `replay_raw_items` operation
refuses that CHECK (23514) and stays at 0009: the downgrade never deletes an operation.
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
-- B5: geo.gazetteer.distance_km, the haversine on the sphere of radius 6371.0088 km
CREATE FUNCTION geo_km(lat1 float8, lon1 float8, lat2 float8, lon2 float8) RETURNS float8
  LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $$
  SELECT 2 * 6371.0088::float8 * asin(least(1, sqrt(
    power(sin((radians(lat2) - radians(lat1)) / 2), 2)
    + cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lon2 - lon1) / 2), 2))))
$$;""")
    op.execute("""\
-- B5: some candidate of a resolved (unique or ambiguous) entry lies within radius_km, after a bounding box
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
      AND abs(p.c_lon - lon) <= radius_km / (110.5 * cos(radians(least(89, abs(lat) + radius_km / 110.5))))
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
    op.execute("DROP INDEX jobs_list_posted_any;")
    op.execute("DROP INDEX score_targets_unscored_rank;")
