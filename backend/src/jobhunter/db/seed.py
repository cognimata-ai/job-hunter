"""Default settings and the seed rows `migrate` writes (spec §13.1, §19.3 step 6, §7.5).

`settings_v1.json` holds every §13.1 default; decimals are JSON strings. The derived rows (scope, model
policy, execution config, evaluation policy) are immutable and deduplicated by the SHA-256 of the canonical
JSON of the payload as stored (§13.2), so a second `seed_defaults` finds the rows of the first.

The `sources` rows (`SOURCES`) and the `default` instance of each non-ATS source (`DEFAULT_INSTANCES`) are
inserted when absent and never updated. So are rubric v1 and v2 (each found by its version) and prompt-schema
versions 1 and 2 (each found by its hash, with its parser family), §19.3 step 6: no migration inserts them
(§19.3 step 2; plan Deviation 32). `active_config` points at the shipped rubric (v2, M3 R13) and prompt-schema
version 2: a fresh row from its insert, and a NULL pointer from the update after it. A pointer already set is
never changed, so an install M2 seeded keeps rubric v1 and prompt-schema version 1 until Settings activates
the new ones (§13.3). M3's first migration restored the two columns' NOT NULL (expand/contract).

The transition matrix is different: it is derived from code, so `sync_transition_matrix` replaces its rows on
every run instead of freezing them.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from importlib import resources
from typing import Final, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import RowMapping, TextClause
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import TRANSITIONS
from jobhunter.types import JSONValue, get_bool, get_int, get_object, get_str

SETTINGS_SCHEMA_VERSION: Final = 1
# The §21.2 central estimate of the share of drafts that need a critic; the policy row of a first installation
# has no history to replay (§11.7.4).
DEFAULT_PROJECTED_CRITIC_RATE: Final = Decimal("0.450")
# `schedule.anchor_local`, "HH:MM" (§13.1); the settings model checks it with this pattern too
ANCHOR_LOCAL: Final = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_SECONDS_PER_HOUR: Final = 3600
_HASH_COLUMN: Final = {
    "scope_versions": "scope_hash",
    "model_policies": "policy_hash",
    "execution_configs": "config_hash",
}


@dataclass(frozen=True)
class SourceSeed:
    """One `sources` row (§7.5)."""

    id: str
    kind: str
    precedence_rank: int
    can_close: bool
    default_completeness: str


SOURCES: Final[tuple[SourceSeed, ...]] = (
    SourceSeed("jsearch", "paid_search", 2, False, "full"),
    SourceSeed("serpapi", "paid_search", 2, False, "full"),
    SourceSeed("adzuna", "quota_search", 4, False, "partial"),
    SourceSeed("arbeitnow", "firehose", 3, False, "full"),
    *(
        SourceSeed(ats, "ats", 1, True, "full")
        for ats in ("greenhouse", "lever", "ashby", "personio", "recruitee", "smartrecruiters")
    ),
)
# The sources with one `default` instance each; an ATS instance belongs to a watchlist company instead.
DEFAULT_INSTANCES: Final = ("jsearch", "serpapi", "adzuna", "arbeitnow")
_INSERT_SOURCES: Final = (
    "INSERT INTO sources (id, kind, precedence_rank, can_close, default_completeness) "
    "VALUES (:id, :kind, :precedence_rank, :can_close, :default_completeness) ON CONFLICT DO NOTHING"
)
_DEFAULT_INSTANCE_IDS: Final = ", ".join(f"'{source_id}'" for source_id in DEFAULT_INSTANCES)
# Static SQL over the constants above, never input: the test reset runs it on a plain psycopg connection, so
# it has no parameters. Looking first keeps a re-run from burning identity values, as `ensure_id` does.
DEFAULT_INSTANCES_SQL: Final = (
    "INSERT INTO source_instances (source_id, instance_key) "  # noqa: S608
    f"SELECT d.id, 'default' FROM unnest(ARRAY[{_DEFAULT_INSTANCE_IDS}]) AS d(id) "
    "WHERE NOT EXISTS (SELECT 1 FROM source_instances i "
    "WHERE i.source_id = d.id AND i.instance_key = 'default') ON CONFLICT DO NOTHING"
)


def canonical_json(value: JSONValue) -> bytes:
    """The UTF-8 bytes of `value` as JSON with sorted keys and no insignificant whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def content_hash(value: JSONValue) -> bytes:
    """The SHA-256 digest of `canonical_json(value)`: the dedup key of the immutable derived rows."""
    return hashlib.sha256(canonical_json(value)).digest()


def load_settings_v1() -> dict[str, JSONValue]:
    """The default settings document (packaged with the wheel)."""
    text = resources.files("jobhunter.db").joinpath("seed_data", "settings_v1.json").read_text("utf-8")
    return get_object(json.loads(text), "")


def scope_payload(settings: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    """The `scope` object of `settings` without `max_ad_age_days` (an admission field, not scope)."""
    scope = get_object(dict(settings), "scope")
    return {key: value for key, value in scope.items() if key != "max_ad_age_days"}


def local_anchor_to_utc(day: date, hhmm: str, tz: str) -> datetime:
    """`hhmm` ("HH:MM") on `day` in the IANA zone `tz`, as an aware UTC datetime.

    The DST rule (M0 Carry-forward 1): a wall time the zone skips (the spring-forward gap) maps to the first
    valid instant after the gap, the transition itself, so 02:30 on 2026-03-29 in Europe/Berlin is 03:00 CEST,
    01:00 UTC. A wall time the zone repeats (the autumn fold) maps to its first occurrence (`fold=0`), so
    02:30 on 2026-10-25 is 02:30 CEST, 00:30 UTC.
    """
    match = ANCHOR_LOCAL.fullmatch(hhmm)
    if match is None:
        raise ValueError(f"anchor time must be HH:MM, got {hhmm!r}")
    zone = ZoneInfo(tz)
    local = datetime.combine(day, time(int(match[1]), int(match[2])), tzinfo=zone)  # fold=0: first occurrence
    instant = local.astimezone(UTC)  # a wall time that does not round-trip lies in a gap
    if instant.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None):
        return instant
    return _end_of_gap(local, zone)


def _end_of_gap(local: datetime, zone: ZoneInfo) -> datetime:
    """The UTC instant at which the gap that skips the wall time `local` ends: its transition.

    In a gap, `fold=0` reads `local` with the offset before the transition and `fold=1` with the one after it
    (PEP 495), so `local - after` lies before the transition and `local - before` at or after it. The
    transition is the first whole second between the two whose offset is no longer `before`.
    """
    before, after = local.utcoffset(), local.replace(fold=1).utcoffset()
    if before is None or after is None or after <= before:
        raise ValueError(f"{local.isoformat()} is in no gap of {zone.key}")
    wall = local.replace(tzinfo=None)
    start = (wall - after).replace(tzinfo=UTC)
    low, high = 0, int(((wall - before) - (wall - after)).total_seconds())
    while high - low > 1:  # the offset at start + low is `before`, at start + high it is not
        middle = (low + high) // 2
        if (start + timedelta(seconds=middle)).astimezone(zone).utcoffset() == before:
            low = middle
        else:
            high = middle
    return start + timedelta(seconds=high)


def anchor_utc_for(now: datetime, hhmm: str, tz: str) -> datetime:
    """The anchor `hhmm` on the date it is at `now` in the zone `tz` (not the UTC date), as UTC."""
    return local_anchor_to_utc(now.astimezone(ZoneInfo(tz)).date(), hhmm, tz)


def latest_slot_at_or_before(anchor: datetime, interval_s: int, t: datetime) -> datetime:
    """The latest schedule slot (`anchor` plus any whole number of intervals) that is not after `t`."""
    if interval_s <= 0:
        raise ValueError(f"interval_s must be positive, got {interval_s}")
    step = timedelta(seconds=interval_s)
    return anchor + ((t - anchor) // step) * step


async def ensure_id(
    conn: AsyncConnection,
    select: TextClause,
    insert: TextClause,
    select_params: Mapping[str, object],
    insert_params: Mapping[str, object],
) -> int:
    """The id of the row `select` finds, inserting it first when it is absent.

    Looking first keeps a re-run from burning identity values. The insert is `ON CONFLICT DO NOTHING
    RETURNING id`, so a concurrent `migrate` that inserted the row in between makes it return nothing; the
    row is then read again.
    """
    found = (await conn.execute(select, select_params)).scalar()
    if found is None:
        found = (await conn.execute(insert, insert_params)).scalar()
    if found is None:
        found = (await conn.execute(select, select_params)).scalar_one()
    return int(found)


async def ensure_hashed(
    conn: AsyncConnection,
    table: Literal["scope_versions", "model_policies", "execution_configs"],
    payload: dict[str, JSONValue],
) -> int:
    """The id of the `table` row whose hash is the content hash of `payload`, created when absent."""
    hash_column = _HASH_COLUMN[table]  # table and column names are constants of this module, never input
    select = q(f"SELECT id FROM {table} WHERE {hash_column} = :hash")  # noqa: S608
    insert = q(
        f"INSERT INTO {table} ({hash_column}, payload) VALUES (:hash, :payload) "  # noqa: S608
        "ON CONFLICT DO NOTHING RETURNING id",
        jsonb=["payload"],
    )
    digest = content_hash(payload)
    return await ensure_id(conn, select, insert, {"hash": digest}, {"hash": digest, "payload": payload})


_EVALUATION_POLICY = q("SELECT id FROM evaluation_policies WHERE policy_hash = :hash")
_INSERT_EVALUATION_POLICY = q(
    "INSERT INTO evaluation_policies (version_no, policy_hash, payload, projected_critic_rate) "
    "SELECT coalesce(max(version_no), 0) + 1, :hash, :payload, :rate FROM evaluation_policies "
    "ON CONFLICT DO NOTHING RETURNING id",
    jsonb=["payload"],
)


async def ensure_evaluation_policy(
    conn: AsyncConnection, payload: dict[str, JSONValue], *, now: datetime | None = None
) -> int:
    """The id of the `evaluation_policies` row whose hash is the content hash of `payload`, created when
    absent with `version_no = max + 1` and its projected critic rate (§11.7.4 L4480, M3 Task 20): with `now`,
    the replay of its thresholds over the round-0 drafts of the 30 days before `now`
    (`evaluation_policy.replay_projected_critic_rate`), or `DEFAULT_PROJECTED_CRITIC_RATE` when fewer than 50
    exist; without `now` (the migrate seed: a first installation has no history) the central estimate. The
    rate is fixed at the creation: a payload that finds its row keeps the row's rate, and nothing is replayed.

    Two writers that create different policies at once would race for one `version_no`: the loser's insert
    does nothing and its read finds no row (`NoResultFound`). The writers are serialised instead: a settings
    save and a candidate's staging hold `active_config` `FOR UPDATE` (§13.3), and the seed runs under the
    `migrate` lock.
    """
    digest = content_hash(payload)
    found = (await conn.execute(_EVALUATION_POLICY, {"hash": digest})).scalar_one_or_none()
    if found is not None:
        return int(found)
    rate = DEFAULT_PROJECTED_CRITIC_RATE
    if now is not None:
        # Imported here: `scoring.evaluation_policy` imports the settings model, which imports this module.
        from jobhunter.scoring.evaluation_policy import EvaluationPolicy, replay_projected_critic_rate

        replayed = await replay_projected_critic_rate(conn, EvaluationPolicy.from_payload(payload), now=now)
        rate = DEFAULT_PROJECTED_CRITIC_RATE if replayed is None else replayed
    return await ensure_id(
        conn,
        _EVALUATION_POLICY,
        _INSERT_EVALUATION_POLICY,
        {"hash": digest},
        {"hash": digest, "payload": payload, "rate": rate},
    )


async def _shipped_rubric_and_prompt_schema(conn: AsyncConnection) -> tuple[int, int]:
    """The ids of the shipped rubric (`RUBRIC_SHIPPED`, v2) and of the shipped prompt-schema version 2. Rubric
    v1 and v2 are each inserted when absent, found by their version; prompt-schema versions 1 (M2's) and 2
    (M3's) each by its hash, with the parser family that reads its generator outputs (`generator_v1`,
    `generator_v2_items`): the seed is the one writer of version rows and of the family, a stored constant
    (plan Deviation 40). The schemas are written as `json` in the order `prompt_schema_v1()` and
    `prompt_schema_v2()` give them, which a structured output follows (Ruling T4-a; the hash is over sorted
    canonical JSON, so the order does not change it). An install M2 left keeps its version 1 row as it is (its
    family is the column's default, `generator_v1`).

    A release whose templates or schemas differ ships a new prompt-schema version this way; it never activates
    itself (§13.3). Imported here, not at the top: the rubric loader and the renderer import this module's
    `content_hash` and `canonical_json`.
    """
    from jobhunter.llm.prompts.render import prompt_schema_hash, prompt_schema_v1, prompt_schema_v2
    from jobhunter.scoring.rubric import RUBRIC_SHIPPED, RUBRIC_V1, RUBRIC_V2, load_rubric

    rubric_ids: dict[str, int] = {}
    for version in (RUBRIC_V1, RUBRIC_V2):
        rubric = load_rubric(version)
        rubric_ids[version] = await ensure_id(
            conn,
            q("SELECT id FROM rubric_versions WHERE version = :version"),
            q(
                "INSERT INTO rubric_versions (version, content_hash, weights, anchors) "
                "VALUES (:version, :hash, :weights, :anchors) ON CONFLICT DO NOTHING RETURNING id",
                jsonb=["weights", "anchors"],
            ),
            {"version": version},
            {
                "version": version,
                "hash": rubric.content_hash,
                "weights": dict(rubric.weights),
                "anchors": dict(rubric.anchors),
            },
        )
    prompt_schema_ids: list[int] = []
    for (templates, schemas), family in (
        (prompt_schema_v1(), "generator_v1"),
        (prompt_schema_v2(), "generator_v2_items"),
    ):
        digest = prompt_schema_hash(templates, schemas)
        prompt_schema_ids.append(
            await ensure_id(
                conn,
                q("SELECT id FROM prompt_schema_versions WHERE prompt_schema_hash = :hash"),
                q(
                    "INSERT INTO prompt_schema_versions (prompt_schema_hash, templates, schemas, "
                    "parser_family) VALUES (:hash, :templates, :schemas, :family) "
                    "ON CONFLICT DO NOTHING RETURNING id",
                    jsonb=["templates"],
                    # `json`, in the code's key order: a structured output follows it (Ruling T4-a)
                    json=["schemas"],
                ),
                {"hash": digest},
                {"hash": digest, "templates": templates, "schemas": schemas, "family": family},
            )
        )
    return rubric_ids[RUBRIC_SHIPPED], prompt_schema_ids[-1]


_STORED_SETTINGS_V1 = q("SELECT id, payload FROM settings_versions WHERE version_no = 1")


async def _stored_settings_v1(conn: AsyncConnection) -> RowMapping | None:
    return (await conn.execute(_STORED_SETTINGS_V1)).mappings().first()


async def _insert_settings_v1(conn: AsyncConnection) -> None:
    """Derive the rows of the packaged defaults and insert settings version 1 over them."""
    settings = load_settings_v1()

    scope_id = await ensure_hashed(conn, "scope_versions", scope_payload(settings))
    model_id = await ensure_hashed(conn, "model_policies", get_object(settings, "scoring.model_policy"))
    execution_id = await ensure_hashed(conn, "execution_configs", get_object(settings, "scoring.execution"))

    evaluation_id = await ensure_evaluation_policy(conn, get_object(settings, "evaluation"))

    await conn.execute(
        q(
            "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, "
            "model_policy_id, execution_config_id, evaluation_policy_id, effect_classes) "
            "VALUES (1, :schema_version, :payload, :scope_id, :model_id, :execution_id, :evaluation_id, "
            "'{none}') ON CONFLICT DO NOTHING",
            jsonb=["payload"],
        ),
        {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "payload": settings,
            "scope_id": scope_id,
            "model_id": model_id,
            "execution_id": execution_id,
            "evaluation_id": evaluation_id,
        },
    )


async def sync_transition_matrix(conn: AsyncConnection) -> int:
    """Replace the whole `transition_matrix` with the edges of the code registry.

    The matrix is the database's copy of `TRANSITIONS`, which the `assert_transition` trigger checks. Code is
    the source, so unlike the frozen settings the rows are rewritten on every run (a release that adds or
    removes an edge takes effect at the next `migrate`), and a row of a table that is no longer registered
    does not survive. Returns the number of edges written. The caller owns the transaction, so a reader sees
    the old matrix or the new one, never a mix.
    """
    insert = q(
        "INSERT INTO transition_matrix (table_name, from_state, to_state) "
        "VALUES (:table_name, :from_state, :to_state)"
    )
    edges = [
        {"table_name": name, "from_state": from_state, "to_state": to_state}
        for name, spec in sorted(TRANSITIONS.items())
        for from_state, to_state in sorted(spec.allowed)
    ]
    await conn.execute(q("DELETE FROM transition_matrix"))
    if edges:  # an empty parameter list would run the INSERT once with no values
        await conn.execute(insert, edges)
    return len(edges)


async def seed_defaults(conn: AsyncConnection, *, now: datetime) -> None:
    """Write the default settings chain, rubric v1 and v2 and prompt-schema versions 1 and 2, the three single
    rows, `sources` and the default source instances.

    Idempotent and never an overwrite: a NULL rubric or prompt-schema pointer is set (to the shipped rubric v2
    and prompt-schema version 2), a set one is never changed (a shipped version activates through Settings
    only, §13.3).
    Settings version 1 is frozen once stored: the derived rows (scope, model policy, execution config,
    evaluation policy) and the version are created from the packaged defaults only when version 1 does not
    exist yet. Afterwards a release that edits `settings_v1.json`
    changes nothing here, and the three single rows below are created, when absent, from the STORED version 1
    and the pointers it holds (`settings_versions` carries all four: scope, model policy, execution config and
    evaluation policy), never from the file. So a second call with another `now` changes nothing. The one
    exception is the transition matrix, which is code-derived and replaced on every call
    (`sync_transition_matrix`). The caller owns the transaction.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    stored = await _stored_settings_v1(conn)
    if stored is None:
        await _insert_settings_v1(conn)
        stored = await _stored_settings_v1(conn)
        if stored is None:
            raise LookupError("settings version 1 is missing right after its insert")
    settings_id = int(stored["id"])
    settings = get_object(stored["payload"], "")

    # The active pointers are those of the settings version they activate, so the two cannot disagree, plus
    # the shipped rubric (v2) and prompt-schema version (2). The profile and tuple pointers stay NULL until a
    # profile is confirmed (§12.6).
    rubric_id, prompt_schema_id = await _shipped_rubric_and_prompt_schema(conn)
    await conn.execute(
        q(
            "INSERT INTO active_config (id, settings_version_id, scope_version_id, model_policy_id, "
            "execution_config_id, evaluation_policy_id, rubric_version_id, prompt_schema_version_id) "
            "SELECT 1, id, scope_version_id, model_policy_id, execution_config_id, evaluation_policy_id, "
            ":rubric_id, :prompt_schema_id FROM settings_versions WHERE id = :settings_id "
            "ON CONFLICT DO NOTHING"
        ),
        {"settings_id": settings_id, "rubric_id": rubric_id, "prompt_schema_id": prompt_schema_id},
    )
    # An install upgraded from M1 has the row with both pointers NULL: they are set here, and only where NULL.
    await conn.execute(
        q(
            "UPDATE active_config SET rubric_version_id = coalesce(rubric_version_id, :rubric_id), "
            "prompt_schema_version_id = coalesce(prompt_schema_version_id, :prompt_schema_id), "
            "updated_at = now() "
            "WHERE id = 1 AND (rubric_version_id IS NULL OR prompt_schema_version_id IS NULL)"
        ),
        {"rubric_id": rubric_id, "prompt_schema_id": prompt_schema_id},
    )

    interval_s = get_int(settings, "schedule.interval_hours") * _SECONDS_PER_HOUR
    anchor_utc = anchor_utc_for(
        now, get_str(settings, "schedule.anchor_local"), get_str(settings, "display.timezone")
    )
    await conn.execute(
        q(
            "INSERT INTO schedule_state (id, anchor_utc, interval_s, enabled, last_evaluated_slot, "
            "settings_version_id) VALUES (1, :anchor_utc, :interval_s, :enabled, :last_slot, :settings_id) "
            "ON CONFLICT DO NOTHING"
        ),
        {
            "anchor_utc": anchor_utc,
            "interval_s": interval_s,
            "enabled": get_bool(settings, "schedule.enabled"),
            "last_slot": latest_slot_at_or_before(anchor_utc, interval_s, now),
            "settings_id": settings_id,
        },
    )

    await conn.execute(q("INSERT INTO ranking_state (id, generation) VALUES (1, 0) ON CONFLICT DO NOTHING"))

    await conn.execute(q(_INSERT_SOURCES), [asdict(source) for source in SOURCES])
    await conn.execute(q(DEFAULT_INSTANCES_SQL))

    await sync_transition_matrix(conn)
