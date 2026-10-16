"""Default settings and the seed rows `migrate` writes (spec §13.1, §19.3 step 6).

`settings_v1.json` holds every §13.1 default; decimals are JSON strings. The derived rows (scope, model
policy, execution config, evaluation policy) are immutable and deduplicated by the SHA-256 of the canonical
JSON of the payload as stored (§13.2), so a second `seed_defaults` finds the rows of the first.

The transition matrix is different: it is derived from code, so `sync_transition_matrix` replaces its rows on
every run instead of freezing them.
"""

import hashlib
import json
import re
from collections.abc import Mapping
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
_ANCHOR_LOCAL: Final = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_SECONDS_PER_HOUR: Final = 3600
_HASH_COLUMN: Final = {
    "scope_versions": "scope_hash",
    "model_policies": "policy_hash",
    "execution_configs": "config_hash",
}


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
    """`hhmm` ("HH:MM") on `day` in the IANA zone `tz`, as an aware UTC datetime."""
    match = _ANCHOR_LOCAL.fullmatch(hhmm)
    if match is None:
        raise ValueError(f"anchor time must be HH:MM, got {hhmm!r}")
    local = datetime.combine(day, time(int(match[1]), int(match[2])), tzinfo=ZoneInfo(tz))
    return local.astimezone(UTC)


def anchor_utc_for(now: datetime, hhmm: str, tz: str) -> datetime:
    """The anchor `hhmm` on the date it is at `now` in the zone `tz` (not the UTC date), as UTC."""
    return local_anchor_to_utc(now.astimezone(ZoneInfo(tz)).date(), hhmm, tz)


def latest_slot_at_or_before(anchor: datetime, interval_s: int, t: datetime) -> datetime:
    """The latest schedule slot (`anchor` plus any whole number of intervals) that is not after `t`."""
    if interval_s <= 0:
        raise ValueError(f"interval_s must be positive, got {interval_s}")
    step = timedelta(seconds=interval_s)
    return anchor + ((t - anchor) // step) * step


async def _ensure_id(
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


async def _ensure_hashed(
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
    return await _ensure_id(conn, select, insert, {"hash": digest}, {"hash": digest, "payload": payload})


_STORED_SETTINGS_V1 = q("SELECT id, payload FROM settings_versions WHERE version_no = 1")


async def _stored_settings_v1(conn: AsyncConnection) -> RowMapping | None:
    return (await conn.execute(_STORED_SETTINGS_V1)).mappings().first()


async def _insert_settings_v1(conn: AsyncConnection) -> None:
    """Derive the rows of the packaged defaults and insert settings version 1 over them."""
    settings = load_settings_v1()

    scope_id = await _ensure_hashed(conn, "scope_versions", scope_payload(settings))
    model_id = await _ensure_hashed(conn, "model_policies", get_object(settings, "scoring.model_policy"))
    execution_id = await _ensure_hashed(conn, "execution_configs", get_object(settings, "scoring.execution"))

    evaluation = get_object(settings, "evaluation")
    evaluation_digest = content_hash(evaluation)
    evaluation_id = await _ensure_id(
        conn,
        q("SELECT id FROM evaluation_policies WHERE policy_hash = :hash"),
        q(
            "INSERT INTO evaluation_policies (version_no, policy_hash, payload, projected_critic_rate) "
            "VALUES (1, :hash, :payload, :rate) ON CONFLICT DO NOTHING RETURNING id",
            jsonb=["payload"],
        ),
        {"hash": evaluation_digest},
        {"hash": evaluation_digest, "payload": evaluation, "rate": DEFAULT_PROJECTED_CRITIC_RATE},
    )

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
    """Replace the `transition_matrix` rows of every registered table with the edges of the code registry.

    The matrix is the database's copy of `TRANSITIONS`, which the `assert_transition` trigger checks. Code is
    the source, so unlike the frozen settings the rows are rewritten on every run (a release that adds or
    removes an edge takes effect at the next `migrate`). Returns the number of edges written. The caller owns
    the transaction, so a reader sees the old matrix or the new one, never a mix.
    """
    delete = q("DELETE FROM transition_matrix WHERE table_name = :table_name")
    insert = q(
        "INSERT INTO transition_matrix (table_name, from_state, to_state) "
        "VALUES (:table_name, :from_state, :to_state)"
    )
    written = 0
    for name, spec in sorted(TRANSITIONS.items()):
        await conn.execute(delete, {"table_name": name})
        edges = [
            {"table_name": name, "from_state": from_state, "to_state": to_state}
            for from_state, to_state in sorted(spec.allowed)
        ]
        await conn.execute(insert, edges)
        written += len(edges)
    return written


async def seed_defaults(conn: AsyncConnection, *, now: datetime) -> None:
    """Write the default settings chain, `active_config`, `schedule_state` and `ranking_state`.

    Idempotent and never an overwrite. Settings version 1 is frozen once stored: the derived rows (scope,
    model policy, execution config, evaluation policy) and the version are created from the packaged
    defaults only when version 1 does not exist yet. Afterwards a release that edits `settings_v1.json`
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

    # The active pointers are those of the settings version they activate, so the two cannot disagree.
    # The profile, tuple, rubric and prompt-schema pointers stay NULL until M2.
    await conn.execute(
        q(
            "INSERT INTO active_config (id, settings_version_id, scope_version_id, model_policy_id, "
            "execution_config_id, evaluation_policy_id) "
            "SELECT 1, id, scope_version_id, model_policy_id, execution_config_id, evaluation_policy_id "
            "FROM settings_versions WHERE id = :settings_id ON CONFLICT DO NOTHING"
        ),
        {"settings_id": settings_id},
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

    await sync_transition_matrix(conn)
