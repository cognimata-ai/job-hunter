from uuid import uuid4

import pytest
import sqlalchemy as sa
from jobhunter.db.seed import sync_transition_matrix
from jobhunter.db.session import make_null_engine
from jobhunter.db.sql import CHECK_VIOLATION, pg_error
from jobhunter.db.transitions import TRANSITIONS, transition
from jobhunter.errors import TransitionConflict

from tests.support.pg import MANUAL_RUN_SQL

pytestmark = pytest.mark.integration
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
    async with engine.connect() as c:
        for name, spec in TRANSITIONS.items():
            have = {row[0] for row in await c.execute(columns, {"t": name})}
            assert spec.columns <= have, name
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
