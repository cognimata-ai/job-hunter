import asyncio

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jobhunter.db.session import make_engine
from jobhunter.db.transitions import TRANSITIONS, transition
from jobhunter.errors import TransitionConflict

from tests.support.pg import MANUAL_RUN_SQL

pytestmark = pytest.mark.integration
STATES = ["queued", "running", "succeeded", "partial", "failed", "failed_invariant", "cancelled"]
STEP = st.tuples(
    st.lists(st.sampled_from(STATES), min_size=1, max_size=3, unique=True), st.sampled_from(STATES)
)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(STEP, min_size=1, max_size=12))
def test_transition_calls_match_the_matrix(migrated, clean_db, make_config, steps) -> None:
    spec = TRANSITIONS["runs"]  # migrated: schema and active_config exist; clean_db: no rows leak in or out

    async def scenario() -> None:
        engine = make_engine(make_config("api", migrated))
        async with engine.connect() as c:  # autobegin; each example rolls back below
            rid = (await c.execute(sa.text(MANUAL_RUN_SQL))).scalar_one()
            state, attempt = "queued", 0
            for froms, to in steps:
                legal = all((f, to) in spec.allowed for f in froms)
                if not legal:
                    with pytest.raises(ValueError):
                        await transition(c, "runs", rid, froms, to)
                    continue
                extra = (
                    {
                        "attempt_no": attempt + 1,
                        "heartbeat_at": sa.func.now(),
                        "current_thread_id": f"run-{rid}-a{attempt + 1}",
                    }
                    if to == "running"
                    else {}
                )
                if state in froms:
                    await transition(c, "runs", rid, froms, to, set_=extra)
                    state, attempt = to, attempt + (1 if to == "running" else 0)
                else:
                    with pytest.raises(TransitionConflict):
                        await transition(c, "runs", rid, froms, to, set_=extra)
                assert (
                    await c.execute(sa.text("SELECT status FROM runs WHERE id=:i"), {"i": rid})
                ).scalar_one() == state
            await c.rollback()
        await engine.dispose()

    asyncio.run(scenario())
