from uuid import uuid4

import pytest
import sqlalchemy as sa
from jobhunter.db.seed import sync_transition_matrix
from jobhunter.db.session import make_null_engine
from jobhunter.db.sql import CHECK_VIOLATION, pg_error
from jobhunter.db.transitions import (
    CLEAR_REASON,
    TRANSITIONS,
    transition,
    transition_set_clause,
    transition_where,
)
from jobhunter.errors import TransitionConflict

from tests.support.m2 import insert_profile_chain
from tests.support.m3 import (
    CLAIM_FROM,
    CLAIM_RETURNING,
    CLAIM_SET_SQL,
    CLAIM_WHERE,
    NOW,
    insert_job,
    insert_published,
    insert_target,
)
from tests.support.pg import MANUAL_RUN_SQL

pytestmark = pytest.mark.integration
INSUFFICIENT_PRIVILEGE = "42501"
INS = sa.text(MANUAL_RUN_SQL)
OP = sa.text(
    "INSERT INTO operations (kind,payload,dedup_key,status) VALUES ('export','{}','e','pending') RETURNING id"
)


async def test_cas_success_conflict_and_owner(engine) -> None:
    async with engine.begin() as c:
        rid = (await c.execute(INS)).scalar_one()
        await transition(
            c,
            "runs",
            rid,
            ["queued"],
            "running",
            set_={"attempt_no": 1, "heartbeat_at": sa.func.now(), "current_thread_id": f"run-{rid}-a1"},
        )
        assert tuple(
            (
                await c.execute(
                    sa.text("SELECT status, started_at IS NOT NULL FROM runs WHERE id=:i"), {"i": rid}
                )
            ).one()
        ) == ("running", True)
        with pytest.raises(TransitionConflict):
            await transition(c, "runs", rid, ["queued"], "running")
        with pytest.raises(TransitionConflict):
            await transition(c, "runs", rid, ["running"], "succeeded", owner={"attempt_no": 2})
        await transition(
            c,
            "runs",
            rid,
            ["running"],
            "succeeded",
            owner={"attempt_no": 1},
            reason="done",
            set_={"report": {"noop": True, "ü": [1]}},
        )
        assert tuple(
            (
                await c.execute(
                    sa.text("SELECT status_reason, finished_at IS NOT NULL, report FROM runs WHERE id=:i"),
                    {"i": rid},
                )
            ).one()
        ) == ("done", True, {"noop": True, "ü": [1]})


@pytest.mark.parametrize(
    ("to", "reason"),
    [("succeeded", None), ("failed", "boom"), ("cancelled", "superseded"), ("pending", "retry")],
)
async def test_exit_from_running_clears_ownership(engine, to, reason) -> None:
    async with engine.begin() as c:
        op = (await c.execute(OP)).scalar_one()
        await transition(
            c,
            "operations",
            op,
            ["pending"],
            "running",
            set_={"claim_id": sa.func.gen_random_uuid(), "worker_epoch": 1, "heartbeat_at": sa.func.now()},
        )
        await transition(c, "operations", op, ["running"], to, reason=reason)
        row = (
            await c.execute(
                sa.text("SELECT status, claim_id, worker_epoch, heartbeat_at FROM operations WHERE id=:i"),
                {"i": op},
            )
        ).one()
        assert tuple(row) == (to, None, None, None)


async def test_helper_refuses_unknown_edge_owner_key_and_cleared_set(engine) -> None:
    async with engine.begin() as c:
        with pytest.raises(ValueError):
            await transition(c, "runs", 1, ["succeeded"], "running")
        with pytest.raises(ValueError):
            await transition(c, "runs", 1, ["queued"], "running", owner={"claim_id": "x"})
        with pytest.raises(ValueError):
            await transition(c, "operations", 1, ["running"], "succeeded", set_={"claim_id": None})


async def test_trigger_rejects_disallowed_direct_update(engine) -> None:
    async with engine.begin() as c:
        rid = (await c.execute(INS)).scalar_one()
        await c.execute(
            sa.text("UPDATE runs SET status='cancelled', finished_at=now() WHERE id=:i"), {"i": rid}
        )
        with pytest.raises(
            sa.exc.IntegrityError, match="disallowed transition runs: cancelled -> queued"
        ) as bad:
            await c.execute(
                sa.text("UPDATE runs SET status='queued', finished_at=NULL WHERE id=:i"), {"i": rid}
            )
        assert pg_error(bad.value).sqlstate == CHECK_VIOLATION


def start(rid: int, attempt: int) -> dict[str, object]:
    return {
        "attempt_no": attempt,
        "heartbeat_at": sa.func.now(),
        "current_thread_id": f"run-{rid}-a{attempt}",
    }


async def test_operation_owner_is_matched_whether_given_as_uuid_or_text(engine) -> None:
    async with engine.begin() as c:
        op = (await c.execute(OP)).scalar_one()
        claim = uuid4()
        await transition(
            c,
            "operations",
            op,
            ["pending"],
            "running",
            set_={"claim_id": claim, "worker_epoch": 1, "heartbeat_at": sa.func.now()},
        )
        for stale in (uuid4(), str(uuid4())):
            with pytest.raises(TransitionConflict):
                await transition(c, "operations", op, ["running"], "succeeded", owner={"claim_id": stale})
        await transition(c, "operations", op, ["running"], "succeeded", owner={"claim_id": str(claim)})
        status = (
            await c.execute(sa.text("SELECT status FROM operations WHERE id=:i"), {"i": op})
        ).scalar_one()
        assert status == "succeeded"


async def test_a_self_edge_keeps_the_first_start_time_and_the_first_entry_sets_it(engine) -> None:
    async with engine.begin() as c:
        rid = (await c.execute(INS)).scalar_one()
        await transition(c, "runs", rid, ["queued", "running"], "running", set_=start(rid, 1))
        entered = "SELECT started_at = now() FROM runs WHERE id=:i"
        assert (await c.execute(sa.text(entered), {"i": rid})).scalar_one() is True
        # Back-dated, because every `now()` of one transaction is the same instant.
        await c.execute(
            sa.text("UPDATE runs SET started_at = now() - interval '1 hour' WHERE id=:i"), {"i": rid}
        )
        await transition(c, "runs", rid, ["queued", "running"], "running", set_=start(rid, 2))
        row = await c.execute(
            sa.text("SELECT started_at < now() - interval '30 minutes', attempt_no FROM runs WHERE id=:i"),
            {"i": rid},
        )
        assert tuple(row.one()) == (True, 2)


async def test_registry_columns_and_triggers_exist_in_the_database(engine) -> None:
    columns = sa.text("SELECT column_name FROM information_schema.columns WHERE table_name=:t")
    triggers = sa.text(
        "SELECT count(*) FROM pg_trigger WHERE tgrelid = CAST(:t AS regclass) "
        "AND tgfoid = 'assert_transition'::regproc AND NOT tgisinternal"
    )
    # Keyed by `(provider, window_start)`: `transition()` never updates it, so it has no `id` (M1 Task 1).
    without_id = {"quota_windows"}
    async with engine.connect() as c:
        for name, spec in TRANSITIONS.items():
            have = {row[0] for row in await c.execute(columns, {"t": name})}
            assert spec.columns - ({"id"} if name in without_id else set()) <= have, name
            assert ("id" in have) is (name not in without_id), name
            assert (await c.execute(triggers, {"t": name})).scalar_one() == 1, name


async def test_the_database_matrix_is_the_registry_and_a_sync_replaces_stale_rows(migrated) -> None:
    expected = {(name, f, t) for name, spec in TRANSITIONS.items() for f, t in spec.allowed}
    matrix = sa.text("SELECT table_name, from_state, to_state FROM transition_matrix")
    engine = make_null_engine(migrated.dsn("migrator", driver="sqlalchemy"))
    try:
        async with engine.connect() as c:
            assert {tuple(row) for row in await c.execute(matrix)} == expected
            await c.execute(
                sa.text("DELETE FROM transition_matrix WHERE table_name='runs' AND to_state='failed'")
            )
            await c.execute(sa.text("INSERT INTO transition_matrix VALUES ('runs', 'succeeded', 'running')"))
            # A row of a table that is no longer registered must not survive a sync either.
            await c.execute(sa.text("INSERT INTO transition_matrix VALUES ('ghost', 'a', 'b')"))
            assert {tuple(row) for row in await c.execute(matrix)} != expected
            assert await sync_transition_matrix(c) == len(expected)
            assert {tuple(row) for row in await c.execute(matrix)} == expected
            await c.rollback()
    finally:
        await engine.dispose()


async def test_trigger_rejects_a_disallowed_direct_update_of_an_operation(engine) -> None:
    async with engine.begin() as c:
        op = (await c.execute(OP)).scalar_one()
        with pytest.raises(
            sa.exc.IntegrityError, match="disallowed transition operations: pending -> succeeded"
        ) as bad:
            await c.execute(
                sa.text("UPDATE operations SET status='succeeded', finished_at=now() WHERE id=:i"), {"i": op}
            )
        assert pg_error(bad.value).sqlstate == CHECK_VIOLATION


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO transition_matrix VALUES ('runs', 'succeeded', 'running')",
        "UPDATE transition_matrix SET to_state = 'running'",
        "DELETE FROM transition_matrix",
        "TRUNCATE transition_matrix",
    ],
)
async def test_the_app_role_cannot_edit_the_matrix_the_trigger_trusts(engine, statement) -> None:
    async with engine.connect() as c:
        with pytest.raises(sa.exc.ProgrammingError) as denied:
            await c.execute(sa.text(statement))
        assert pg_error(denied.value).sqlstate == INSUFFICIENT_PRIVILEGE
        await c.rollback()


async def test_the_app_role_reads_the_matrix_and_a_legal_transition_still_passes_the_trigger(engine) -> None:
    async with engine.begin() as c:
        edges = (await c.execute(sa.text("SELECT count(*) FROM transition_matrix"))).scalar_one()
        assert edges == sum(len(spec.allowed) for spec in TRANSITIONS.values())
        rid = (await c.execute(INS)).scalar_one()
        await transition(
            c, "runs", rid, ["queued"], "cancelled"
        )  # an UPDATE of status: the trigger reads the matrix
        status = (await c.execute(sa.text("SELECT status FROM runs WHERE id=:i"), {"i": rid})).scalar_one()
        assert status == "cancelled"


async def test_clear_reason_empties_the_reason_column_and_no_reason_leaves_it(engine) -> None:
    reason = sa.text("SELECT status, status_reason FROM runs WHERE id=:i")
    async with engine.begin() as c:
        rid = (await c.execute(INS)).scalar_one()
        await c.execute(sa.text("UPDATE runs SET status_reason = 'redeferred' WHERE id=:i"), {"i": rid})
        await transition(c, "runs", rid, ["queued"], "running", reason=CLEAR_REASON, set_=start(rid, 1))
        assert tuple((await c.execute(reason, {"i": rid})).one()) == ("running", None)
        await c.execute(sa.text("UPDATE runs SET status_reason = 'kept' WHERE id=:i"), {"i": rid})
        await transition(c, "runs", rid, ["running"], "succeeded", owner={"attempt_no": 1})
        assert tuple((await c.execute(reason, {"i": rid})).one()) == ("succeeded", "kept")


CLAIMED = sa.text(
    "SELECT id, state, claim_id, claim_epoch, attempt_no, lane, always_include_reason, "
    "claimed_at = now() AS claimed_now, claim_heartbeat_at = now() AS heartbeat_now, "
    "state_changed_at = now() AS changed_now FROM score_targets WHERE id = ANY(CAST(:ids AS bigint[])) "
    "ORDER BY id"
)


async def test_transition_where_moves_only_matching_rows_and_the_trigger_checks_each(engine) -> None:
    async with engine.connect() as c:
        tuple_id = (await insert_profile_chain(c)).llm_tuple_id
        # Back-dated, because every `now()` of one transaction is the same instant.
        pending = [
            await insert_target(c, await insert_job(c), tuple_id, attempt_no=n, state_changed_at=NOW)
            for n in (0, 1, 2)
        ]
        held = uuid4()
        busy = await insert_target(
            c,
            await insert_job(c),
            tuple_id,
            state="in_progress",
            state_changed_at=NOW,
            claim_id=held,
            claim_epoch=1,
            claimed_at=NOW,
            claim_heartbeat_at=NOW,
            attempt_no=5,
            lane="similarity",
        )
        ids = [*pending, busy]
        lanes = ["similarity", "oldest", "always_include", "exploration"]
        reasons = [None, None, "watchlist", "title_match"]
        rows = await transition_where(
            c,
            "score_targets",
            ["pending"],
            "in_progress",
            where=CLAIM_WHERE,
            from_sql=CLAIM_FROM,
            params={"ids": ids, "lanes": lanes, "reasons": reasons, "epoch": 2, "max_attempts": 4},
            set_sql=CLAIM_SET_SQL,
            returning=CLAIM_RETURNING,
        )
        assert sorted(row["id"] for row in rows) == pending
        assert all(set(row) == {"id", "claim_id", "attempt_no"} for row in rows)
        claims = {row["id"]: row["claim_id"] for row in rows}
        assert None not in claims.values()
        assert len(set(claims.values())) == 3  # gen_random_uuid() per row
        assert {row["id"]: row["attempt_no"] for row in rows} == dict(zip(pending, (1, 2, 3), strict=True))
        stored = [tuple(row) for row in await c.execute(CLAIMED, {"ids": ids})]
        assert stored == [
            (pending[0], "in_progress", claims[pending[0]], 2, 1, "similarity", None, True, True, True),
            (pending[1], "in_progress", claims[pending[1]], 2, 2, "oldest", None, True, True, True),
            (
                pending[2],
                "in_progress",
                claims[pending[2]],
                2,
                3,
                "always_include",
                "watchlist",
                True,
                True,
                True,
            ),
            # not pending: the in-progress target keeps its claim, its attempt and its lane
            (busy, "in_progress", held, 1, 5, "similarity", None, False, False, False),
        ]

        # A where that selects a published row moves nothing: the state is part of the predicate.
        published = (await insert_published(c, await insert_job(c), tuple_id)).target_id
        claim = {"claim_id": uuid4(), "claim_epoch": 2, "claim_heartbeat_at": NOW}
        assert (
            await transition_where(
                c,
                "score_targets",
                ["pending"],
                "in_progress",
                where="t.id = :id",
                params={"id": published},
                set_=claim,
            )
            == []
        )
        state = sa.text("SELECT state FROM score_targets WHERE id = :id")
        assert (await c.execute(state, {"id": published})).scalar_one() == "published_accepted"

        # The trigger checks every row of a set-based UPDATE: one legal row does not carry an illegal one.
        fresh = await insert_target(c, await insert_job(c), tuple_id)
        with pytest.raises(
            sa.exc.IntegrityError,
            match="disallowed transition score_targets: published_accepted -> in_progress",
        ) as bad:
            async with c.begin_nested():
                await c.execute(
                    sa.text(
                        "UPDATE score_targets SET state = 'in_progress', claim_id = gen_random_uuid(), "
                        "claim_epoch = 2, claim_heartbeat_at = now() WHERE id = ANY(CAST(:ids AS bigint[]))"
                    ),
                    {"ids": [fresh, published]},
                )
        assert pg_error(bad.value).sqlstate == CHECK_VIOLATION
        assert (await c.execute(state, {"id": fresh})).scalar_one() == "pending"
        await c.rollback()


REVIVE = (
    "INSERT INTO score_targets (job_revision_id, llm_tuple_id, job_id, state) "
    "VALUES (:revision, :tuple, :job, :state) "
    "ON CONFLICT (job_revision_id, llm_tuple_id) DO UPDATE SET {clause} "
    "WHERE score_targets.state = 'cancelled' "
    "AND score_targets.state_reason IN ('tuple_superseded', 'revision_superseded', 'job_merged') RETURNING id"
)
REVIVED = sa.text(
    "SELECT state, state_reason, override, override_at, lane, state_changed_at = now() FROM score_targets "
    "WHERE id = :id"
)


async def test_the_revive_upsert_moves_a_cancelled_target_through_the_set_clause(engine) -> None:
    clause = transition_set_clause(
        "score_targets",
        "pending",
        reason=CLEAR_REASON,
        set_sql={"override": "'none'", "override_at": "NULL", "lane": "NULL", "updated_at": "now()"},
    )
    async with engine.connect() as c:
        tuple_id = (await insert_profile_chain(c)).llm_tuple_id
        job = await insert_job(c)
        target = await insert_target(
            c,
            job,
            tuple_id,
            state="cancelled",
            state_reason="tuple_superseded",
            state_changed_at=NOW,
            override="score_now",
            override_at=NOW,
            lane="score_now",
        )
        params = {"revision": job.revision_id, "tuple": tuple_id, "job": job.job_id, "state": "pending"}
        revived = await c.execute(sa.text(REVIVE.format(clause=clause)), params)
        assert revived.scalar_one() == target
        assert tuple((await c.execute(REVIVED, {"id": target})).one()) == (
            "pending",
            None,
            "none",
            None,
            None,
            True,
        )
        await c.rollback()
