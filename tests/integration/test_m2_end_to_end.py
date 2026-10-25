"""Task 24: M2 acceptance in process, on the fake LLM (the hand-authored synthetic `extract_profile` and
`propose_queries` streams of Tasks 13 and 20), the fake embedder and the source fixtures: a pasted CV through
the text review, the extraction, the edit and the confirm, the profile vectors and the query proposal, the
accepted queries enabled by a settings save, a scope save and its apply, and a manual collect run.

Every request goes through the api (`logged_in_client`); the operations run in process (`op_runner`, with the
fake embedder as its worker's), and the run on the real collection graph (`execute_run`). The CV is
`SYNTHETIC_CV`, an invented candidate; no real CV and no paid call.
"""

import dataclasses
import json
from collections.abc import Callable, Mapping
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.graphs.collect.graph import build_collect_graph
from jobhunter.llm import budget, spend_ledger
from jobhunter.worker import runs
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.profile import (
    SYNTHETIC_CV,
    active_profile,
    draft_row,
    draft_row_api,
    idem,
    ranking_generation,
)
from tests.support.settings import (
    active_scope_id,
    fresh_key,
    pending_scope_id,
    save,
    save_scope,
    scope_ids_of,
)

pytestmark = pytest.mark.integration

# What contact stripping replaces in `SYNTHETIC_CV`: the header name and the e-mail address.
STRIP_REPORT: Final = {"email": 1, "phone": 0, "url": 0, "address": 0, "personal": 0, "name": 1}
# The span the review removes: the certification line, which the profile does not need.
REMOVED_LINE: Final = "Zertifikate: AWS Certified Developer (2021)"
REMOVED: Final = (SYNTHETIC_CV.index(REMOVED_LINE), SYNTHETIC_CV.index(REMOVED_LINE) + len(REMOVED_LINE))
# The one field edit before the confirm: a third target title, so the version has four vectors.
ADDED_TITLE: Final = {"title": "Platform Engineer", "lang": "en"}
VECTOR_KINDS: Final = ["summary", "title:0", "title:1", "title:2"]
ACCEPTED: Final = 6  # of the proposal's eight items (`SYNTHETIC_QUERIES`)
RADIUS_KM: Final = 40
# Words of the CV (the stripped name and e-mail address among them) that no log line, checkpoint row or
# operation payload may hold, compared case-folded.
CV_MARKERS: Final = ("Musterfrau", "Musterfirma", "Zahlungsdienst", "Datenplattform", "Musterstadt")
CHECKPOINT_TABLES: Final = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
NON_SCORING_PURPOSES: Final = ["extract_profile", "propose_queries"]


async def rows(engine: AsyncEngine, statement: str, **params: object) -> list[sa.Row[Any]]:
    async with engine.connect() as conn:
        return list((await conn.execute(sa.text(statement), params)).all())


def as_text(row: Mapping[Any, Any]) -> str:
    """Every column of a row as one case-folded text (bytes decoded leniently, the rest as JSON)."""
    parts = []
    for value in row.values():
        if isinstance(value, bytes | memoryview):
            parts.append(bytes(value).decode("utf-8", errors="replace"))
        else:
            parts.append(json.dumps(value, default=str, ensure_ascii=False))
    return "\x1f".join(parts).casefold()


async def contents_outside_the_profile(
    engine: AsyncEngine, log_events: Callable[[], list[dict[str, Any]]]
) -> dict[str, list[str]]:
    """The logged events, every checkpoint row and every operation payload, each as case-folded text."""
    found: dict[str, list[str]] = {
        "logs": [json.dumps(e, ensure_ascii=False).casefold() for e in log_events()]
    }
    async with engine.connect() as conn:
        for table in CHECKPOINT_TABLES:
            result = await conn.execute(sa.text(f"SELECT * FROM {table}"))  # noqa: S608 - constant names
            found[table] = [as_text(row._mapping) for row in result]
        result = await conn.execute(sa.text("SELECT kind, payload FROM operations"))
        found["operations.payload"] = [as_text(row._mapping) for row in result]
    return found


async def confirm(client: httpx.AsyncClient, draft_id: int) -> dict[str, Any]:
    r = await client.post(
        f"/api/profile/drafts/{draft_id}/confirm",
        json={"expected_rescore_ack": True},
        headers=idem(fresh_key("confirm")),
    )
    assert r.status_code == 201, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


async def accept(client: httpx.AsyncClient, proposal_id: int, item: dict[str, Any]) -> int:
    """`POST /api/queries` of a proposal's item (201 required): the new query's id."""
    r = await client.post(
        "/api/queries",
        json={
            "text": item["text"],
            "lang": item["lang"],
            "proposal_id": proposal_id,
            "exploration": item["exploration"],
        },
    )
    assert r.status_code == 201, (r.status_code, r.text)
    assert (r.json()["origin"], r.json()["proposal_id"]) == ("llm_proposal", proposal_id)
    return int(r.json()["id"])


async def test_profile_and_settings_flow_on_the_fake_llm(
    logged_in_client, engine, op_runner, upload, current_epoch, fake_http, collect_deps, log_events
) -> None:
    client = logged_in_client
    # The worker's embedder: the fake one, as the collection's.
    op_runner.deps = dataclasses.replace(op_runner.deps, embedder=collect_deps.embedder)

    # 1. The paste reaches the text review with its contacts stripped.
    draft_id = await upload(SYNTHETIC_CV)
    await op_runner.run_pending()
    draft = await draft_row_api(client, draft_id)
    assert (draft["status"], draft["strip_report"]) == ("text_review", STRIP_REPORT)

    # 2. One span removed, the text approved, the profile extracted.
    r = await client.put(
        f"/api/profile/drafts/{draft_id}/text",
        json={"span_edits": [{"op": "remove", "start": REMOVED[0], "end": REMOVED[1]}]},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    assert "[REMOVED]" in r.json()["stripped_text_preview"]
    assert REMOVED_LINE not in r.json()["stripped_text_preview"]
    r = await client.post(f"/api/profile/drafts/{draft_id}/text-review", headers=idem(fresh_key("approve")))
    assert r.status_code == 202, (r.status_code, r.text)
    await op_runner.run_pending()
    stored = await draft_row(engine, draft_id)
    assert stored.status == "ready" and REMOVED_LINE not in stored.reviewed_text
    draft = await draft_row_api(client, draft_id)

    # 3. One field edited, then the confirm: version 1, its tuple, and the next ranking generation.
    edited = {**draft["draft"], "target_titles": [*draft["draft"]["target_titles"], ADDED_TITLE]}
    r = await client.put(f"/api/profile/drafts/{draft_id}", json={"draft": edited})
    assert r.status_code == 200, (r.status_code, r.text)
    generation = await ranking_generation(engine)
    confirmed = await confirm(client, draft_id)
    version_id = confirmed["profile_version_id"]
    assert (confirmed["targets_to_create"], confirmed["est_usd"]) == (0, "0.000000")  # no ad retained yet
    listed = (await client.get("/api/profile")).json()
    assert (listed["active"]["id"], listed["active"]["version_no"]) == (version_id, 1)
    assert listed["active"]["profile"]["target_titles"][-1] == ADDED_TITLE
    active_version, tuple_id = await active_profile(engine)
    (tuple_row,) = await rows(engine, "SELECT id, profile_version_id FROM llm_tuples")
    assert (active_version, tuple_id, tuple_row.profile_version_id) == (version_id, tuple_row.id, version_id)
    assert await ranking_generation(engine) == generation + 1

    # 4. The confirm's operations: four vectors, and a ready proposal.
    await op_runner.run_pending()
    vectors = await rows(
        engine, "SELECT kind FROM profile_vectors WHERE profile_version_id = :v ORDER BY kind", v=version_id
    )
    assert [v.kind for v in vectors] == VECTOR_KINDS
    (proposal,) = await rows(
        engine, "SELECT id FROM query_proposals WHERE profile_version_id = :v", v=version_id
    )
    r = await client.get(f"/api/profile/query-proposals/{proposal.id}")
    assert r.json()["status"] == "ready" and 6 <= len(r.json()["items"]) <= 16
    items = r.json()["items"]

    # 5. Six items accepted, then enabled by a settings save that acknowledges the dry-run's warnings.
    query_ids = [await accept(client, proposal.id, item) for item in items[:ACCEPTED]]
    payload = (await client.get("/api/settings")).json()["payload"]
    payload["queries"] = [{"query_id": query_id, "enabled": True} for query_id in query_ids]
    saved = await save(client, payload)
    assert saved["version"]["effect_classes"] == ["collection"]
    cells = await rows(
        engine,
        "SELECT query_id, count(*) AS n FROM coverage_cells WHERE query_id = ANY(:q) GROUP BY query_id",
        q=query_ids,
    )
    assert sorted(c.query_id for c in cells) == sorted(query_ids) and all(c.n > 0 for c in cells)

    # 6. A scope save (one location at a radius), its apply, and the flip.
    scoped = await save_scope(client, radius_km=RADIUS_KM)
    (scope_id,) = await scope_ids_of(engine, scoped)
    assert scoped["apply_scope_operation_id"] is not None and await pending_scope_id(engine) == scope_id
    await op_runner.run_pending()
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (scope_id, None)
    (apply_op,) = await rows(
        engine, "SELECT status FROM operations WHERE id = :i", i=scoped["apply_scope_operation_id"]
    )
    assert apply_op.status == "succeeded"

    # 7. A manual collect run on the fake sources succeeds, with I5 on the active tuple.
    r = await client.post("/api/runs", json={"include_paid": False}, headers=idem(fresh_key("run-now")))
    assert r.status_code == 202 and r.json()["created"] is True, (r.status_code, r.text)
    run_id = int(r.json()["run"]["id"])
    compiled = build_collect_graph(InMemorySaver(), collect_deps)
    assert await runs.execute_run(engine, compiled, run_id, 1, epoch=current_epoch) == "started"
    (run,) = await rows(engine, "SELECT status, status_reason, report FROM runs WHERE id = :r", r=run_id)
    assert (run.status, run.status_reason) == ("succeeded", None)
    assert run.report["execution"]["invariants"]["I5"] == {
        "ok": True,
        "detail": {"llm_tuple_id": tuple_id, "targets": "m3"},
    }
    assert all(v["ok"] for v in run.report["execution"]["invariants"].values())
    fetched = await rows(
        engine,
        "SELECT DISTINCT c.query_id FROM fetch_requests f JOIN coverage_cells c ON c.id = f.cell_id "
        "WHERE f.run_id = :r AND f.status = 'succeeded' AND c.query_id IS NOT NULL",
        r=run_id,
    )
    assert sorted(f.query_id for f in fetched) == sorted(query_ids)  # the saved queries drove the run

    # 8. The month's spend: the two billed non-scoring calls, and counters that equal the ledger.
    ledger = await rows(engine, "SELECT purpose, purpose_class, state FROM llm_spend_ledger ORDER BY purpose")
    assert [tuple(e) for e in ledger] == [(p, "nonscoring", "billed") for p in NON_SCORING_PURPOSES]
    spend = (await client.get("/api/system/spend")).json()
    assert sorted((g["role"], g["calls"]) for g in spend["by_model_effort_role"]) == [
        (p, 1) for p in NON_SCORING_PURPOSES
    ]
    assert spend["counted"] == spend["billed"] != "0.000000"
    assert (spend["ambiguous"], spend["reserved"]) == ("0.000000", "0.000000")
    month = budget.month_of(utcnow())
    async with engine.connect() as conn:
        assert (await conn.execute(sa.text("SELECT 1 FROM llm_budget WHERE month = :m"), {"m": month})).one()
        assert await spend_ledger.recompute(conn, month) is None

    # 9. No CV word outside the profile's own tables.
    found = await contents_outside_the_profile(engine, log_events)
    assert found["logs"] and found["checkpoints"] and found["operations.payload"]
    for where, texts in found.items():
        for marker in CV_MARKERS:
            assert [t for t in texts if marker.casefold() in t] == [], (where, marker)
