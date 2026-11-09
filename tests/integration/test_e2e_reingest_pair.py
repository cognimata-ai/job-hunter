"""E10's prerequisite (plan M4 Task 32): the fixtures' Adzuna re-ingest pair (`adzuna_reingest_pair`, one ad
under two ids) defers under the E2E setup's scope and profile, in process as `test_m3_end_to_end.py` runs the
score lane's side. The E2E settings saved as step 3 saves them (Berlin at 30 km, `batch_bulk_rescore = false`,
the scope applied), the E2E inputs collected on the fixtures with the paid sources (the query `software
engineer` and the five boards), the fake extraction's profile of `SYNTHETIC_CV` confirmed and its
`create_targets_for_tuple` run: the pair is one active group whose member's target is `deferred_duplicate` in
it, whose representative's is `pending`, and both jobs pass the active scope's hard filters, as
`e2e_setup.reingest_problem` checks the rows of `e2e_setup.REINGEST_PAIR` (step 9's own check).

Every datum is a scrubbed fixture or invented.
"""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
import sqlalchemy as sa

from tests.support.collect import SOURCE_SWITCHES, run_to_the_end, seed_inputs
from tests.support.profile import idem, make_ready_draft
from tests.support.settings import fresh_key, save, settings_body

pytestmark = pytest.mark.integration

SCRIPTS: Final = Path(__file__).resolve().parents[2] / "scripts"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """scripts/e2e_setup.py with `m4_seeds` and the smoke modules beside it, as it runs as a script; no cached
    copy of any stays behind (`test_smoke_m3_script.import_smoke`)."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("smoke_m0", "smoke_m1", "smoke_m2", "smoke_m3", "m4_seeds"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, name, load(name))
    return load("e2e_setup")


async def test_the_reingest_pair_defers_under_the_e2e_scope_and_profile(
    engine, current_epoch, logged_in_client, op_runner, upload, fake_llm, collect_deps, fake_embedder, setup
) -> None:
    smoke_m1, smoke_m3 = sys.modules["smoke_m1"], sys.modules["smoke_m3"]
    client = logged_in_client
    # Step 3's save: Berlin at 30 km and no batch re-scoring, then the scope apply.
    active = await settings_body(client)
    payload: dict[str, Any] = copy.deepcopy(active["payload"])
    payload["scope"]["locations"] = [setup.wanted_location()]
    payload["scoring"]["batch_bulk_rescore"] = False
    saved = await save(client, payload, base=int(active["version"]["id"]))
    assert isinstance(saved["apply_scope_operation_id"], int)
    await op_runner.run_pending()
    assert setup.scope_is_wanted((await settings_body(client))["payload"])

    # Steps 2 and 4: the E2E inputs (the Lever board on its EU site, as `smoke_m3.SEED_BOARDS` has it),
    # collected with the paid sources on the fixtures.
    boards = [("greenhouse", smoke_m1.BOARD_SLUG), *((ats, slug) for ats, slug, _ in smoke_m3.BOARDS)]
    await seed_inputs(
        engine, queries=(smoke_m1.QUERY_TEXT,), companies=boards, enabled=SOURCE_SWITCHES, lever_region="eu"
    )
    await run_to_the_end(engine, collect_deps, include_paid=True, companies=(), enabled=SOURCE_SWITCHES)

    # Step 6: the fake extraction's profile confirmed, and its targets.
    draft_id = await make_ready_draft(client, op_runner, upload)
    confirmed = await client.post(
        f"/api/profile/drafts/{draft_id}/confirm",
        json={"expected_rescore_ack": True},
        headers=idem(fresh_key("confirm")),
    )
    assert confirmed.status_code == 201, (confirmed.status_code, confirmed.text)
    await op_runner.run_pending()
    async with engine.connect() as conn:
        (status,) = (
            await conn.execute(
                sa.text(
                    "SELECT status FROM operations WHERE kind = 'create_targets_for_tuple' "
                    "AND (payload->>'llm_tuple_id')::bigint = "
                    "(SELECT llm_tuple_id FROM active_config WHERE id = 1)"
                )
            )
        ).one()
        pair = json.loads((await conn.execute(sa.text(setup.REINGEST_PAIR))).scalar_one())
    assert status == "succeeded"
    assert setup.reingest_problem(pair) is None, setup.reingest_problem(pair)
    states = sorted((row["target_state"], row["deferred_group_id"] is not None) for row in pair)
    assert states == [("deferred_duplicate", True), ("pending", False)]
