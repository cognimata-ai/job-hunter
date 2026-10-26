"""`transition_where` over random score-target edges, on the database.

Each example draws a registered edge, up to three more from-states with an edge into its to-state, a reason,
and random settable columns given by value (`set_`) or by SQL (`set_sql`); it seeds targets in random states,
some selected by the `where` and some not, runs the helper once and compares every column of every seeded row
with what the registry says the edge does: the moved rows (in a from-state and selected) get the state, the
reason, the changed-at column (kept on a self-edge), `NULL` in the columns the exit clears and the set
columns; no other column of any row changes.

The columns a to-state's CHECKs require come with every edge into it (`_required`), and a seeded row carries
what its own state requires (`_seeded`). A published target names its result through a deferred foreign key,
which the example never reaches: it rolls back before COMMIT, so the result ids here name no `llm_results`.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jobhunter.db.session import make_engine
from jobhunter.db.transitions import CLEAR_REASON, TRANSITIONS, _Clear, transition_where
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.support.m2 import insert_profile_chain
from tests.support.m3 import active_ids, insert_bulk_operation, insert_id, insert_job, insert_submission

pytestmark = pytest.mark.integration
SPEC: Final = TRANSITIONS["score_targets"]
STATES: Final = sorted({state for edge in SPEC.allowed for state in edge})
EDGES: Final = sorted(SPEC.allowed)
# The states whose CHECK wants a reason (`failed_%`, and the three named ones).
NEEDS_REASON: Final = frozenset(
    {state for state in STATES if state.startswith("failed_")}
    | {"expired_unscored", "cancelled", "budget_deferred"}
)
LANES: Final = (
    "similarity",
    "oldest",
    "exploration",
    "always_include",
    "score_now",
    "batch",
    "batch_followup",
)
# Seeded rows are older than the transaction, so a changed-at column that the edge sets visibly moves.
SEEDED_AT: Final = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
PUBLISHED_OFFSET: Final = 2_000_000_000_000

_TIMES = st.datetimes(
    min_value=datetime(2020, 1, 1),  # noqa: DTZ001 (hypothesis takes naive bounds with `timezones`)
    max_value=datetime(2030, 1, 1),  # noqa: DTZ001
    timezones=st.just(UTC),
)
_TEXT = st.text(alphabet="abcdefghij_", min_size=1, max_size=8)
# The settable columns with no foreign key and no CHECK that ties them to the state. `claim_id` is not one:
# with `claim_epoch` and `claim_heartbeat_at` it would make a target outside `in_progress` look claimed.
FREE: Final[Mapping[str, st.SearchStrategy[object]]] = {
    "claimed_at": _TIMES,
    "claim_epoch": st.integers(1, 10**6),
    "claim_heartbeat_at": _TIMES,
    "attempt_no": st.integers(0, 50),
    "lane": st.sampled_from((None, *LANES)),
    "always_include_reason": st.sampled_from(
        (None, "watchlist", "title_match", "full_text_twin", "batch_followup")
    ),
    "charged_attempts": st.integers(0, 4),
    "not_before": st.one_of(st.none(), _TIMES),
    "transient_failures": st.integers(0, 50),
    "refusal_category": _TEXT,
    "updated_at": _TIMES,
}
# Columns given as SQL over `t`, with the value each yields for a row (`:bump` is a caller bind).
EXPRESSIONS: Final = {
    "attempt_no": "t.attempt_no + :bump",
    "transient_failures": "t.transient_failures + 1",
}
# What each to-state's CHECKs require of the row the edge leaves, beyond the state.
_REQUIRED_KEYS: Final = {
    "in_progress": ("claim_id", "claim_epoch", "claim_heartbeat_at"),
    "in_batch": ("batch_submission_id",),
    "deferred_duplicate": ("deferred_group_id",),
    "filtered_out": ("filter_scope_version_id",),
    "refused": ("refusal_category",),
    "published_accepted": ("published_result_id",),
    "published_contested": ("published_result_id",),
}


@dataclass(frozen=True)
class Row:
    state: str
    selected: bool
    attempt_no: int
    transient_failures: int
    override: bool


@dataclass(frozen=True)
class Case:
    to: str
    froms: tuple[str, ...]
    reason: str | _Clear | None
    set_: Mapping[str, object]
    set_sql: tuple[str, ...]
    bump: int
    claim: tuple[UUID, int, datetime]
    refusal: str
    rows: tuple[Row, ...]


def cleared(froms: tuple[str, ...], to: str) -> frozenset[str]:
    """The columns `clear_on_exit` lists for every from-state other than the to-state."""
    return frozenset(column for f in froms if f != to for column in SPEC.clear_on_exit.get(f, ()))


@st.composite
def cases(draw: st.DrawFn) -> Case:
    # A registered edge, and up to three more from-states with an edge into the same to-state.
    first, to = draw(st.sampled_from(EDGES))
    others = sorted(f for f, t in SPEC.allowed if t == to and f != first)
    froms = (first, *draw(st.lists(st.sampled_from(others), unique=True, max_size=3))) if others else (first,)
    reason = draw(_TEXT) if to in NEEDS_REASON else draw(st.one_of(st.none(), st.just(CLEAR_REASON), _TEXT))
    free = sorted(set(FREE) - cleared(froms, to) - set(_REQUIRED_KEYS.get(to, ())))
    chosen = draw(st.lists(st.sampled_from(free), unique=True)) if free else []
    set_: dict[str, object] = {column: draw(FREE[column]) for column in chosen}
    if draw(st.booleans()):  # the override pair: the CHECK `(override = 'none') = (override_at IS NULL)`
        override = draw(st.sampled_from(("none", "score_now", "score_anyway", "individual")))
        set_["override"] = override
        set_["override_at"] = None if override == "none" else draw(_TIMES)
    by_sql = sorted(set(EXPRESSIONS) - set(set_))
    set_sql = tuple(draw(st.lists(st.sampled_from(by_sql), unique=True))) if by_sql else ()
    row = st.builds(
        Row,
        state=st.one_of(st.sampled_from(froms), st.sampled_from(STATES)),
        selected=st.booleans(),
        attempt_no=st.integers(0, 50),
        transient_failures=st.integers(0, 50),
        override=st.booleans(),
    )
    return Case(
        to=to,
        froms=froms,
        reason=reason,
        set_=set_,
        set_sql=set_sql,
        bump=draw(st.integers(1, 5)),
        claim=(draw(st.uuids()), draw(st.integers(1, 10**6)), draw(_TIMES)),
        refusal=draw(_TEXT),
        rows=tuple(draw(st.lists(row, min_size=1, max_size=6))),
    )


@dataclass(frozen=True)
class Scaffold:
    """The rows a target's state may name: a batch submission, a duplicate group and a scope version."""

    llm_tuple_id: int
    submission: int
    group: int
    scope: int


async def scaffold(c: AsyncConnection) -> Scaffold:
    tuple_id = (await insert_profile_chain(c)).llm_tuple_id
    bulk = await insert_bulk_operation(c, tuple_id)
    submission = await insert_submission(c, tuple_id, bulk)
    representative = await insert_job(c)
    group = await insert_id(
        c,
        "duplicate_groups",
        {"representative_job_id": representative.job_id, "status": "active", "election_reason": "initial"},
    )
    return Scaffold(tuple_id, submission, group, (await active_ids(c)).scope_version_id)


def _seeded(row: Row, n: int, ids: Scaffold) -> dict[str, object]:
    """The columns of a target seeded in `row.state`, with what that state's CHECKs require."""
    values: dict[str, object] = {
        "state": row.state,
        "state_changed_at": SEEDED_AT,
        "attempt_no": row.attempt_no,
        "transient_failures": row.transient_failures,
    }
    if row.override:
        values |= {"override": "score_now", "override_at": SEEDED_AT}
    if row.state in NEEDS_REASON:
        values["state_reason"] = "seeded"
    match row.state:
        case "in_progress":
            values |= {
                "claim_id": UUID(int=n + 1),
                "claim_epoch": 1,
                "claimed_at": SEEDED_AT,
                "claim_heartbeat_at": SEEDED_AT,
            }
        case "in_batch":
            values["batch_submission_id"] = ids.submission
        case "deferred_duplicate":
            values["deferred_group_id"] = ids.group
        case "filtered_out":
            values["filter_scope_version_id"] = ids.scope
        case "refused":
            values["refusal_category"] = "seeded"
        case "published_accepted" | "published_contested":
            values["published_result_id"] = PUBLISHED_OFFSET // 2 + n
    return values


def _required(case: Case, ids: Scaffold) -> tuple[dict[str, object], dict[str, str]]:
    """The `set_` values and the `set_sql` expressions the to-state's CHECKs require."""
    match case.to:
        case "in_progress":
            claim_id, epoch, heartbeat = case.claim
            return {"claim_id": claim_id, "claim_epoch": epoch, "claim_heartbeat_at": heartbeat}, {}
        case "in_batch":
            return {"batch_submission_id": ids.submission}, {}
        case "deferred_duplicate":
            return {"deferred_group_id": ids.group}, {}
        case "filtered_out":
            return {"filter_scope_version_id": ids.scope}, {}
        case "refused":
            return {"refusal_category": case.refusal}, {}
        case "published_accepted" | "published_contested":
            return {}, {"published_result_id": f"t.id + {PUBLISHED_OFFSET}"}
    return {}, {}


SNAPSHOT: Final = sa.text("SELECT * FROM score_targets WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id")


async def snapshot(c: AsyncConnection, ids: list[int]) -> dict[int, dict[str, Any]]:
    return {int(row["id"]): dict(row) for row in (await c.execute(SNAPSHOT, {"ids": ids})).mappings()}


def expected_row(
    case: Case, set_: Mapping[str, object], before: Mapping[str, Any], now: datetime
) -> dict[str, Any]:
    """What the registry says the edge does to `before`, a moved row."""
    after = dict(before)
    after["state"] = case.to
    if isinstance(case.reason, str):
        after["state_reason"] = case.reason
    elif case.reason is CLEAR_REASON:
        after["state_reason"] = None
    if before["state"] != case.to:  # a self-edge keeps the time of the first entry
        after["state_changed_at"] = now
    after |= dict.fromkeys(cleared(case.froms, case.to))
    after |= set_
    if "attempt_no" in case.set_sql:
        after["attempt_no"] = before["attempt_no"] + case.bump
    if "transient_failures" in case.set_sql:
        after["transient_failures"] = before["transient_failures"] + 1
    if case.to.startswith("published_"):
        after["published_result_id"] = before["id"] + PUBLISHED_OFFSET
    return after


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(cases())
def test_transition_where_moves_exactly_the_matching_rows_and_columns(
    migrated, clean_db, make_config, case
) -> None:
    async def scenario() -> None:
        engine = make_engine(make_config("api", migrated))
        try:
            async with engine.connect() as c:  # autobegin; each example rolls back below
                ids = await scaffold(c)
                seeded: list[int] = []
                for n, row in enumerate(case.rows):
                    job = await insert_job(c)
                    values = {
                        "job_revision_id": job.revision_id,
                        "llm_tuple_id": ids.llm_tuple_id,
                        "job_id": job.job_id,
                        **_seeded(row, n, ids),
                    }
                    seeded.append(await insert_id(c, "score_targets", values))
                rows = dict(zip(seeded, case.rows, strict=True))
                moved = {target for target, row in rows.items() if row.selected and row.state in case.froms}
                before = await snapshot(c, seeded)
                now = (await c.execute(sa.text("SELECT now()"))).scalar_one()

                required, required_sql = _required(case, ids)
                set_ = {**case.set_, **required}
                params: dict[str, object] = {"ids": [target for target, row in rows.items() if row.selected]}
                if "attempt_no" in case.set_sql:
                    params["bump"] = case.bump
                returned = await transition_where(
                    c,
                    "score_targets",
                    case.froms,
                    case.to,
                    where="t.id = ANY(CAST(:ids AS bigint[]))",
                    params=params,
                    reason=case.reason,
                    set_=set_,
                    set_sql={column: EXPRESSIONS[column] for column in case.set_sql} | required_sql,
                    returning=("id", "state"),
                )

                assert {row["id"] for row in returned} == moved
                assert {row["state"] for row in returned} <= {case.to}
                after = await snapshot(c, seeded)
                for target in seeded:
                    want = (
                        expected_row(case, set_, before[target], now) if target in moved else before[target]
                    )
                    assert after[target] == want, (target, case)
                await c.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
