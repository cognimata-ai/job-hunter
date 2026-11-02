"""The M4 schema (migration 0009): the index of the unscored section (B1) and of the posted sort over every
availability (B6), the radius functions `geo_km` and `job_within_km` (B5), and the `replay_raw_items`
operation kind (Task 12). 0009 adds no view and no row, and its downgrade is exact.
"""

import asyncio
import importlib
import random
import re
from typing import Final, get_args

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from jobhunter.db.alembic_runner import alembic_config, code_head
from jobhunter.db.sql import q
from jobhunter.geo.gazetteer import distance_km
from jobhunter.types import JSONValue
from jobhunter.worker.operations import OperationKind
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.support.pg import PgServer, pg_one, pg_rows
from tests.support.schema import refused_check

pytestmark = pytest.mark.integration
M3_SCHEMA: Final = "0008_m3_scoring"
M4_SCHEMA: Final = "0009_m4_ui"
INDEXES: Final = {
    "score_targets_unscored_rank": (
        "CREATE INDEX score_targets_unscored_rank ON public.score_targets USING btree "
        "(llm_tuple_id, similarity DESC NULLS LAST, job_id DESC) WHERE (state <> ALL (ARRAY["
        "'published_accepted'::text, 'published_contested'::text, 'deferred_duplicate'::text, "
        "'cancelled'::text]))"
    ),
    "jobs_list_posted_any": (
        "CREATE INDEX jobs_list_posted_any ON public.jobs USING btree "
        "(posted_at_effective DESC NULLS LAST, id DESC) WHERE (merged_into_job_id IS NULL)"
    ),
}
_INDEXES: Final = "SELECT indexname, indexdef FROM pg_indexes WHERE indexname = ANY (%s) ORDER BY indexname"
_FUNCTIONS: Final = (
    "SELECT proname, pg_get_function_identity_arguments(oid), prorettype::regtype::text, provolatile, "
    "proisstrict, proparallel FROM pg_proc WHERE proname IN ('geo_km', 'job_within_km') ORDER BY proname"
)
# name, arguments, result type, provolatile ('i' IMMUTABLE), proisstrict, proparallel ('s' SAFE)
FUNCTIONS: Final = [
    (
        "geo_km",
        "lat1 double precision, lon1 double precision, lat2 double precision, lon2 double precision",
        "double precision",
        "i",
        True,
        "s",
    ),
    (
        "job_within_km",
        "locations jsonb, lat double precision, lon double precision, radius_km double precision",
        "boolean",
        "i",
        True,
        "s",
    ),
]
_KIND_CHECK: Final = (
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'operations'::regclass "
    "AND conname = 'operations_kind_check'"
)


def test_0009_follows_0008_and_is_the_head() -> None:
    module = importlib.import_module(f"jobhunter.db.migrations.versions.{M4_SCHEMA}")
    assert (module.revision, module.down_revision) == (M4_SCHEMA, M3_SCHEMA)
    assert code_head() == M4_SCHEMA


def test_0009_creates_the_two_indexes_and_the_functions(migrated: PgServer) -> None:
    assert pg_rows(migrated, "migrator", _INDEXES, list(INDEXES)) == sorted(INDEXES.items())
    assert pg_rows(migrated, "migrator", _FUNCTIONS) == FUNCTIONS


async def test_geo_km_equals_distance_km(engine) -> None:
    """100 random pairs within Germany's bounding box, the same seed every run (resolution (e))."""
    rng = random.Random(9)  # noqa: S311 - seeded for the same 100 pairs every run, not a secret
    pairs = [
        (
            rng.uniform(47.27, 55.06),
            rng.uniform(5.87, 15.04),
            rng.uniform(47.27, 55.06),
            rng.uniform(5.87, 15.04),
        )
        for _ in range(100)
    ]
    statement = q(
        "SELECT geo_km(a, b, c, d) FROM unnest(CAST(:a AS float8[]), CAST(:b AS float8[]), "
        "CAST(:c AS float8[]), CAST(:d AS float8[])) WITH ORDINALITY AS p(a, b, c, d, n) ORDER BY n"
    )
    columns = {name: [pair[i] for pair in pairs] for i, name in enumerate("abcd")}
    async with engine.connect() as c:
        distances = list((await c.execute(statement, columns)).scalars())
    expected = [distance_km(*pair) for pair in pairs]
    assert len(distances) == 100 and max(expected) > 500  # pairs across the country, not only near ones
    assert all(abs(got - want) <= 1e-6 for got, want in zip(distances, expected, strict=True))
    async with engine.connect() as c:
        same = (await c.execute(q("SELECT geo_km(52.52, 13.405, 52.52, 13.405)"))).scalar_one()
        null = (await c.execute(q("SELECT geo_km(52.52, 13.405, NULL, 13.405)"))).scalar_one()
    assert (same, null) == (0.0, None)  # STRICT


BERLIN: Final = (52.52, 13.405)
MUNICH: Final = (48.137, 11.575)
# A point 20 km north of Berlin: a degree of latitude is 6371.0088 km * pi / 180 on the haversine sphere.
NORTH_20_KM: Final = (BERLIN[0] + 20 / 111.19508, BERLIN[1])


def entry(resolution: str, *points: tuple[float, float]) -> dict[str, JSONValue]:
    """A stored location (`sources.parsing.locations.Location`) with one candidate per point."""
    return {
        "label": "Testort",
        "resolution": resolution,
        "candidates": [
            {"gazetteer_id": f"geonames:{i}", "country_code": "DE", "lat": lat, "lon": lon}
            for i, (lat, lon) in enumerate(points, start=1)
        ],
        "postal_code": None,
        "source": "jsearch",
    }


async def within(c: AsyncConnection, locations: list[JSONValue], radius_km: float) -> bool | None:
    statement = q(
        "SELECT job_within_km(:locations, CAST(:lat AS float8), CAST(:lon AS float8), CAST(:r AS float8))",
        jsonb=["locations"],
    )
    params = {"locations": locations, "lat": BERLIN[0], "lon": BERLIN[1], "r": radius_km}
    result: bool | None = (await c.execute(statement, params)).scalar_one()
    return result


async def test_job_within_km_matches_any_resolved_candidate_only(engine) -> None:
    assert 19.99 < distance_km(*BERLIN, *NORTH_20_KM) < 20.01
    ambiguous = entry("ambiguous", MUNICH, NORTH_20_KM)
    async with engine.connect() as c:
        assert await within(c, [ambiguous], 30) is True  # the second candidate is 20 km away
        assert await within(c, [ambiguous], 10) is False
        assert await within(c, [entry("unique", MUNICH), ambiguous], 30) is True  # any entry
        assert await within(c, [entry("unique", NORTH_20_KM)], 30) is True
        assert await within(c, [entry("unique", MUNICH)], 30) is False
        assert await within(c, [entry("unique", MUNICH)], 520) is True  # Munich is about 504 km away
        assert await within(c, [entry("unresolved", BERLIN)], 30) is False  # coordinates, but unresolved
        assert await within(c, [entry("unresolved", BERLIN), entry("unique", MUNICH)], 30) is False
        assert await within(c, [], 200) is False
        assert await within(c, [entry("ambiguous")], 200) is False  # no candidate
        null = q("SELECT job_within_km(NULL, 52.52, 13.405, 30)")
        assert (await c.execute(null)).scalar_one() is None  # STRICT


async def test_operations_accept_replay_raw_items(engine) -> None:
    insert = (
        "INSERT INTO operations (kind, payload, dedup_key, status) "
        "VALUES (:kind, '{}', :dedup, 'pending') RETURNING kind"
    )
    async with engine.connect() as c:
        params = {"kind": "replay_raw_items", "dedup": "replay_raw_items:1"}
        assert (await c.execute(sa.text(insert), params)).scalar_one() == "replay_raw_items"
        old_kind: dict[str, object] = {"kind": "replay", "dedup": "replay:1"}
        await refused_check(c, "operations", insert, old_kind, "'replay_raw_items'::text")
        await c.rollback()


async def test_the_kind_check_lists_every_operation_kind(engine) -> None:
    """The CHECK and `worker.operations.OperationKind` name the same kinds: 0001's eleven and the new one."""
    async with engine.connect() as c:
        definition = (await c.execute(sa.text(_KIND_CHECK))).scalar_one()
    kinds = re.findall(r"'([a-z_]+)'::text", definition)
    assert kinds == list(get_args(OperationKind)) and kinds[-1] == "replay_raw_items"


def _catalogue(server: PgServer) -> tuple[object, ...]:
    return (
        pg_one(server, "migrator", "SELECT version_num FROM alembic_version"),
        pg_rows(server, "migrator", _INDEXES, list(INDEXES)),
        pg_rows(server, "migrator", _FUNCTIONS),
        pg_one(server, "migrator", _KIND_CHECK),
    )


async def test_0009_downgrades_and_upgrades(fresh_pg_server: PgServer) -> None:
    """The downgrade leaves exactly the 0008 schema: no index, no function, and 0001's kind CHECK as 0001
    wrote it, which refuses the new kind again; upgrading once more restores 0009. With a `replay_raw_items`
    operation stored, the downgrade is refused and the database stays at 0009 with the operation."""
    dsn = fresh_pg_server.dsn("migrator", driver="sqlalchemy")
    await asyncio.to_thread(command.upgrade, alembic_config(dsn), M3_SCHEMA)
    at_0008 = _catalogue(fresh_pg_server)
    assert at_0008[:3] == ((M3_SCHEMA,), [], [])
    await asyncio.to_thread(command.upgrade, alembic_config(dsn), M4_SCHEMA)
    at_0009 = _catalogue(fresh_pg_server)
    assert at_0009[:3] == ((M4_SCHEMA,), sorted(INDEXES.items()), FUNCTIONS)
    assert at_0009[3] != at_0008[3]

    await asyncio.to_thread(command.downgrade, alembic_config(dsn), M3_SCHEMA)
    assert _catalogue(fresh_pg_server) == at_0008
    replay = "INSERT INTO operations (kind, payload, dedup_key, status) VALUES (%s, '{}', 'r:1', 'pending')"
    with pytest.raises(psycopg.Error) as refusal:
        pg_one(fresh_pg_server, "migrator", replay, "replay_raw_items")
    assert (refusal.value.sqlstate, refusal.value.diag.constraint_name) == ("23514", "operations_kind_check")

    await asyncio.to_thread(command.upgrade, alembic_config(dsn), M4_SCHEMA)
    assert _catalogue(fresh_pg_server) == at_0009
    pg_one(fresh_pg_server, "migrator", replay + " RETURNING id", "replay_raw_items")
    with pytest.raises(sa.exc.IntegrityError, match="operations_kind_check"):
        await asyncio.to_thread(command.downgrade, alembic_config(dsn), M3_SCHEMA)
    assert _catalogue(fresh_pg_server) == at_0009
    assert pg_one(fresh_pg_server, "migrator", "SELECT kind FROM operations") == ("replay_raw_items",)
