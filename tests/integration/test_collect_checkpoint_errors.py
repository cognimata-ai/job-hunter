"""The collection graph's node errors in its checkpoints (spec §9.1, §16; final review FR-12).

LangGraph stores the exception that escapes a node in `checkpoint_writes` (its `repr`). PostgreSQL's DETAIL
quotes a row's values (`Key (k)=(…) already exists.`) even with the engine's `hide_parameters`, so every
collection node is wrapped by `graphs.sanitize.sanitized`: an unclassified error reaches LangGraph as
`NodeFailed` (its class only), the run ends as it did before (`failed('bug')`), and `DB_RETRY` still retries a
serialization failure.
"""

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final
from unittest import mock

import pytest
import sqlalchemy as sa
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.collect import graph as collect_graph
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.graph import CollectGraph, CollectUpdate, build_collect_graph
from jobhunter.graphs.collect.nodes.hard_filters import make_hard_filters
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import run_to_the_end

pytestmark = pytest.mark.integration

# A synthetic company name: it must never reach a checkpoint.
MARKER: Final = "Quastenflosser Beispiel GmbH"
CHECKPOINT_TABLES: Final = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
RUN_END = sa.text("SELECT status, status_reason FROM runs WHERE id = :i")


def raise_with_detail(code: str, message: str) -> sa.TextClause:
    """A statement that fails with SQLSTATE `code` and a DETAIL quoting the marker, as PostgreSQL's DETAIL
    quotes a row's values (`Key (company_key)=(…) already exists.`) whatever `hide_parameters` hides. The
    marker is built when the statement runs, so the statement's own text, which the error quotes too, does
    not hold it."""
    return sa.text(
        f"DO $$ BEGIN RAISE EXCEPTION '{message}' USING ERRCODE = '{code}', "
        f"DETAIL = format('Key (company_key)=(%s) already exists.', reverse('{MARKER[::-1]}')); END $$"
    )


async def unique_violation(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(raise_with_detail("23505", "duplicate key value violates unique constraint"))


async def serialization_failure(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(raise_with_detail("40001", "could not serialize access"))


def graph_whose_hard_filters(
    before: Callable[[AsyncEngine, int], Awaitable[None]],
) -> Callable[[BaseCheckpointSaver[str], CollectDeps], CollectGraph]:
    """The real collection graph, whose `hard_filters` node first awaits `before(engine, call_number)`."""

    def factory(saver: BaseCheckpointSaver[str], deps: CollectDeps) -> CollectGraph:
        calls: list[int] = []

        def make(node_deps: CollectDeps) -> Callable[[Any], Awaitable[CollectUpdate]]:
            real = make_hard_filters(node_deps)

            async def hard_filters(state: Any) -> CollectUpdate:
                calls.append(1)
                await before(node_deps.engine, len(calls))
                return await real(state)

            return hard_filters

        with mock.patch(f"{collect_graph.__name__}.make_hard_filters", make):
            return build_collect_graph(saver, deps)

    return factory


async def checkpoint_contents(engine: AsyncEngine) -> Mapping[str, list[bytes]]:
    """Every row of the three checkpoint tables, each as the bytes of all its columns."""
    rows: dict[str, list[bytes]] = {}
    async with engine.connect() as conn:
        for table in CHECKPOINT_TABLES:
            result = await conn.execute(sa.text(f"SELECT * FROM {table}"))  # noqa: S608 - constant names
            rows[table] = [
                b"\x1f".join(
                    bytes(v) if isinstance(v, bytes | memoryview) else json.dumps(v, default=str).encode()
                    for v in row._mapping.values()
                )
                for row in result
            ]
    return rows


async def error_writes(engine: AsyncEngine, run_id: int) -> list[bytes]:
    async with engine.connect() as conn:
        statement = sa.text(
            "SELECT blob FROM checkpoint_writes WHERE thread_id = :t AND channel = '__error__' ORDER BY idx"
        )
        return [bytes(b) for b in (await conn.execute(statement, {"t": f"run-{run_id}-a1"})).scalars()]


async def run_end(engine: AsyncEngine, run_id: int) -> tuple[str, str | None]:
    async with engine.connect() as conn:
        status, reason = (await conn.execute(RUN_END, {"i": run_id})).one()
    return status, reason


async def test_a_database_detail_reaches_no_checkpoint_and_the_run_still_fails_as_a_bug(
    engine, migrated, current_epoch, fake_http, fake_embedder, collect_deps, log_events
) -> None:
    async def fail(node_engine: AsyncEngine, _call: int) -> None:
        await unique_violation(node_engine)

    async with open_saver(migrated.dsn("app")) as saver:
        rid = await run_to_the_end(engine, collect_deps, graph=graph_whose_hard_filters(fail), saver=saver)
    assert await run_end(engine, rid) == ("failed", "bug")  # as the unwrapped node's error ended it
    [stored] = await error_writes(engine, rid)
    assert b"NodeFailed" in stored and b"IntegrityError" in stored  # the stand-in, naming the class only
    contents = await checkpoint_contents(engine)
    assert contents["checkpoints"], "the scan must see the run's checkpoints"
    for table, rows in contents.items():
        assert not [row for row in rows if MARKER.encode() in row], table
    rendered = json.dumps(log_events(), ensure_ascii=False)
    assert MARKER not in rendered
    [failed] = [e for e in log_events() if e["event"] == "collect_node_failed"]
    assert (failed["node"], failed["error_class"]) == ("hard_filters", "IntegrityError")


async def test_a_serialization_failure_is_still_retried_and_its_detail_reaches_no_checkpoint(
    engine, migrated, current_epoch, fake_http, fake_embedder, collect_deps, log_events
) -> None:
    async def fail_once(node_engine: AsyncEngine, call: int) -> None:
        if call == 1:
            await serialization_failure(node_engine)

    async with open_saver(migrated.dsn("app")) as saver:
        rid = await run_to_the_end(
            engine, collect_deps, graph=graph_whose_hard_filters(fail_once), saver=saver
        )
    status, _ = await run_end(engine, rid)
    assert status in ("succeeded", "partial")  # the retry ran the node again, and the run went on
    assert await error_writes(engine, rid) == []
    for table, rows in (await checkpoint_contents(engine)).items():
        assert not [row for row in rows if MARKER.encode() in row], table
    assert MARKER not in json.dumps(log_events(), ensure_ascii=False)
    [failed] = [e for e in log_events() if e["event"] == "collect_node_failed"]
    assert (failed["node"], failed["error_class"]) == ("hard_filters", "OperationalError")
