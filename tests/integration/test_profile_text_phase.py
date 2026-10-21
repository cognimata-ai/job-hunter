"""The operation runtime and the `profile_graph` text phase, `extract_profile_text` (spec §7.16.8, §9.1, §9.5,
§12.2, §12.3; plan Task 11, Deviations 13, 25 and 27).

Every operation runs in process (`op_runner`): a real `OperationDeps` on the fake LLM, a parse client served
by an in-process docparse loop, and the api's CV store. Every name and datum is invented (Max Mustermann,
Erika Musterfrau, example.org); no real CV.
"""

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.crypto import data_keys
from jobhunter.db.transitions import transition
from jobhunter.graphs.profile.graph import thread_id
from jobhunter.graphs.profile.nodes import strip_contacts as strip_node
from jobhunter.graphs.profile.nodes.parse import LOW_TEXT_CHARS
from jobhunter.llm.errors import Refused
from jobhunter.profile.contacts import strip_contacts
from jobhunter.profile.upload import request_draft_operation
from jobhunter.types import JSONValue
from jobhunter.worker import operations as ops
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.faults import PausePoint
from tests.support.fixtures import count_rows
from tests.support.llm import reclaim_stale_now
from tests.support.profile import (
    OpRunner,
    cv_row,
    docx_bytes,
    draft_row,
    encrypted_pdf,
    image_only_pdf,
    minimal_pdf,
    nul_char_pdf,
)

pytestmark = pytest.mark.integration

# An invented word of the synthetic CV, which no checkpoint row, log line or exception text may hold.
MARKER = "Quastenflosser"
CV_LINES = [
    "Max Mustermann",
    "Senior Python Developer",
    "E-Mail: max.mustermann@example.org",
    "Telefon: +49 30 12345678",
    "Musterstraße 12, 10115 Berlin",
    "Berufserfahrung",
    "2019 - 2026 Musterfirma GmbH, Berlin: Backend-Entwicklung mit Python und PostgreSQL",
    "2015 - 2019 Beispiel AG, Hamburg: Datenplattform " + MARKER + " mit Kafka und Spark",
    "Ausbildung",
    "2010 - 2015 Informatik (M.Sc.), Technische Universität Musterstadt",
    "Kenntnisse: Python, FastAPI, SQLAlchemy, PostgreSQL, Docker, Kubernetes",
    "Sprachen: Deutsch (Muttersprache), Englisch (C1)",
]
TWO_PAGE_PDF = minimal_pdf(CV_LINES, lines_per_page=6)
PASTE = "\n".join(CV_LINES)
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def events_named(log_events: Callable[[], list[dict[str, Any]]], event: str) -> list[dict[str, Any]]:
    return [e for e in log_events() if e["event"] == event]


async def one_op(op_runner: OpRunner) -> int:
    [op_id] = await op_runner.due()
    return op_id


async def op_row(engine: AsyncEngine, op_id: int) -> sa.Row[Any]:
    async with engine.connect() as conn:
        statement = sa.text(
            "SELECT status, attempts, last_error_code, not_before, claim_id, xmin::text AS xmin "
            "FROM operations WHERE id = :i"
        )
        return (await conn.execute(statement, {"i": op_id})).one()


async def draft_xmin(engine: AsyncEngine, draft_id: int) -> str:
    """The transaction that last wrote the draft's row (equal for two rows written in one transaction)."""
    async with engine.connect() as conn:
        statement = sa.text("SELECT xmin::text FROM profile_drafts WHERE id = :d")
        return str((await conn.execute(statement, {"d": draft_id})).scalar_one())


async def raw_text(engine: AsyncEngine, op_runner: OpRunner, draft_id: int) -> str:
    cv = await cv_row(engine, draft_id)
    keys = data_keys(op_runner.deps.cfg)
    return op_runner.deps.cv_store.decrypt_text(cv.raw_text_enc, keys, cv_file_id=cv.id)


async def checkpoint_rows(engine: AsyncEngine) -> dict[str, list[bytes]]:
    """Every row of the three checkpoint tables, each as the bytes of all its columns."""
    rows: dict[str, list[bytes]] = {}
    async with engine.connect() as conn:
        for table in CHECKPOINT_TABLES:
            result = await conn.execute(sa.text(f"SELECT * FROM {table}"))  # noqa: S608 - constant names
            rows[table] = [_as_bytes(row._mapping) for row in result]
    return rows


def _as_bytes(row: Mapping[Any, Any]) -> bytes:
    parts = []
    for value in row.values():
        if isinstance(value, bytes | memoryview):
            parts.append(bytes(value))
        else:
            parts.append(json.dumps(value, default=str, ensure_ascii=False).encode())
    return b"\x1f".join(parts)


async def error_writes(engine: AsyncEngine, thread: str) -> list[bytes]:
    """The `__error__` writes LangGraph stored for `thread`: the serialized exception of a failed node."""
    async with engine.connect() as conn:
        statement = sa.text(
            "SELECT blob FROM checkpoint_writes WHERE thread_id = :t AND channel = '__error__' ORDER BY idx"
        )
        return [bytes(blob) for blob in (await conn.execute(statement, {"t": thread})).scalars()]


async def assert_no_cv_text_in_checkpoints(engine: AsyncEngine) -> None:
    rows = await checkpoint_rows(engine)
    assert rows["checkpoints"], "the scan must see the thread's checkpoints"
    for table, contents in rows.items():
        for content in contents:
            assert MARKER.encode() not in content, table
            assert b"max.mustermann" not in content, table


# --- the text phase --------------------------------------------------------------------------------------


async def test_scanned_pdf_ends_low_text_without_any_llm_call(engine, op_runner, upload) -> None:
    draft_id = await upload(image_only_pdf())  # Task 9 route through the test client
    await op_runner.run_pending()  # runs extract_profile_text with an in-process docparse loop
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("failed", "low_text")
    assert await count_rows(engine, "llm_calls") == 0 and await count_rows(engine, "llm_spend_ledger") == 0


async def test_a_two_page_pdf_reaches_text_review_with_its_contacts_stripped(
    engine, op_runner, upload
) -> None:
    draft_id = await upload(TWO_PAGE_PDF)
    [op_id] = await op_runner.run_pending()
    draft, cv = await draft_row(engine, draft_id), await cv_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("text_review", None)
    assert (await op_row(engine, op_id)).status == "succeeded"
    assert (cv.extraction_method, cv.page_count) == ("pypdf", 2)
    raw = await raw_text(engine, op_runner, draft_id)
    assert "max.mustermann@example.org" in raw and len(raw) >= LOW_TEXT_CHARS  # contacts stay in the raw text
    assert "[EMAIL]" in cv.stripped_text and "max.mustermann@example.org" not in cv.stripped_text
    expected = strip_contacts(raw)
    assert cv.stripped_text == expected.stripped_text
    assert cv.strip_report == dict(expected.report) and cv.strip_report["email"] == 1
    assert all(isinstance(count, int) for count in cv.strip_report.values())  # counts, never a value
    assert len(op_runner.docparse_jobs) == 1
    assert await count_rows(engine, "llm_calls") == 0


async def test_a_docx_reaches_text_review(engine, op_runner, upload) -> None:
    draft_id = await upload(docx_bytes(CV_LINES), filename="Lebenslauf.docx")
    await op_runner.run_pending()
    draft, cv = await draft_row(engine, draft_id), await cv_row(engine, draft_id)
    assert draft.status == "text_review"
    assert (cv.extraction_method, cv.page_count) == ("docx", None)
    assert "[EMAIL]" in cv.stripped_text


async def test_pasted_text_reaches_text_review_without_docparse(engine, op_runner, upload) -> None:
    draft_id = await upload(PASTE)
    await op_runner.run_pending()
    draft, cv = await draft_row(engine, draft_id), await cv_row(engine, draft_id)
    assert draft.status == "text_review"
    assert cv.extraction_method == "pasted" and cv.storage_path is None
    assert "[EMAIL]" in cv.stripped_text and cv.strip_report["email"] == 1
    assert op_runner.docparse_jobs == []


async def test_a_nul_in_a_paste_never_reaches_the_database(engine, op_runner, upload) -> None:
    """PostgreSQL's `text` refuses U+0000: the paste is stored without it, and every later text derives from
    that stored raw text (Task 14 rebuilds from it)."""
    draft_id = await upload(PASTE.replace("Berufserfahrung", "Berufs\x00erfahrung"))
    await op_runner.run_pending()
    draft, cv = await draft_row(engine, draft_id), await cv_row(engine, draft_id)
    assert draft.status == "text_review"
    raw = await raw_text(engine, op_runner, draft_id)
    assert "\x00" not in raw and "Berufserfahrung" in raw
    assert "\x00" not in cv.stripped_text


async def test_a_nul_in_a_docparse_result_never_reaches_the_database(engine, op_runner, upload) -> None:
    lines = [line + " €" for line in CV_LINES]  # the PDF's text map reads every "€" as U+0000
    draft_id = await upload(nul_char_pdf(lines))
    await op_runner.run_pending()
    draft, cv = await draft_row(engine, draft_id), await cv_row(engine, draft_id)
    assert draft.status == "text_review"
    assert len(op_runner.docparse_jobs) == 1
    raw = await raw_text(engine, op_runner, draft_id)
    assert "\x00" not in raw and "Musterfirma" in raw
    assert "\x00" not in cv.stripped_text


async def test_an_encrypted_pdf_fails_the_draft(engine, op_runner, upload) -> None:
    draft_id = await upload(encrypted_pdf(minimal_pdf(CV_LINES)))
    [op_id] = await op_runner.run_pending()
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code, draft.status_reason) == ("failed", "encrypted", "encrypted")
    op = await op_row(engine, op_id)
    assert (op.status, op.last_error_code, op.attempts) == ("failed", "encrypted", 0)
    assert (await cv_row(engine, draft_id)).raw_text_enc is None


async def _missing_file(engine: AsyncEngine, op_runner: OpRunner, draft_id: int) -> None:
    op_runner.deps.cv_store.delete((await cv_row(engine, draft_id)).storage_path)


async def _foreign_file(engine: AsyncEngine, op_runner: OpRunner, draft_id: int) -> None:
    """Another file's blob under this file's name: its associated data names the other file."""
    store, keys = op_runner.deps.cv_store, data_keys(op_runner.deps.cfg)
    other = store.write(TWO_PAGE_PDF, keys)
    path = (await cv_row(engine, draft_id)).storage_path
    (store.root / path).write_bytes((store.root / other.storage_path).read_bytes())


async def _foreign_text(engine: AsyncEngine, op_runner: OpRunner, draft_id: int) -> None:
    """A pasted text sealed for another `cv_files` row."""
    cv = await cv_row(engine, draft_id)
    blob = op_runner.deps.cv_store.encrypt_text(PASTE, data_keys(op_runner.deps.cfg), cv_file_id=cv.id + 1)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE cv_files SET raw_text_enc = :b WHERE id = :i"), {"b": blob, "i": cv.id}
        )


@pytest.mark.parametrize(
    ("content", "spoil"),
    [(TWO_PAGE_PDF, _missing_file), (TWO_PAGE_PDF, _foreign_file), (PASTE, _foreign_text)],
    ids=["missing_file", "foreign_file", "foreign_pasted_text"],
)
async def test_an_unreadable_cv_fails_the_draft_and_frees_the_upload_slot(
    engine, op_runner, upload, content, spoil
) -> None:
    """Task 9 carry: a missing or undecryptable CV ends the draft `failed('cv_file_unreadable')`, never
    stuck in `extracting_text`, which would answer 409 to every new upload."""
    draft_id = await upload(content)
    await spoil(engine, op_runner, draft_id)
    [op_id] = await op_runner.run_pending()
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("failed", "cv_file_unreadable")
    op = await op_row(engine, op_id)
    assert (op.status, op.last_error_code) == ("failed", "cv_file_unreadable")
    assert op_runner.docparse_jobs == []
    await upload(PASTE)  # 202: the slot is free


async def test_resume_skips_a_finished_parse(engine, op_runner, upload) -> None:
    draft_id = await upload(TWO_PAGE_PDF)
    op_id = await one_op(op_runner)
    pause = PausePoint("profile.text.after_parse_commit")
    first = asyncio.create_task(op_runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    await reclaim_stale_now(engine, op_id)  # the reconciler takes the stalled execution's claim
    await op_runner.run_one(op_id)  # the second execution, on a thread of its own
    pause.resume()
    await asyncio.wait_for(first, 30)  # its next owner write is fenced: it writes nothing
    draft = await draft_row(engine, draft_id)
    assert draft.status == "text_review"
    op = await op_row(engine, op_id)
    assert (op.status, op.attempts) == ("succeeded", 1)
    assert len(op_runner.docparse_jobs) == 1  # the parse receipt: no second extraction
    second = await op_runner.deps.profile_graphs.text.aget_state(
        {"configurable": {"thread_id": thread_id("t", draft_id, op_id, 1)}}
    )
    assert second.values["steps"] == ["parse", "strip_contacts"]


async def test_two_operations_of_one_draft_use_two_threads(engine, op_runner, upload) -> None:
    """A cancelled operation's execution (a superseding purge) and the draft's next operation never share
    state: the second's steps are its own, though `steps` adds up across a thread."""
    draft_id = await upload(TWO_PAGE_PDF)
    first_op = await one_op(op_runner)
    pause = PausePoint("profile.text.after_parse_commit")
    first = asyncio.create_task(op_runner.run_one(first_op, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    async with engine.begin() as conn:
        assert await ops.cancel_operation(conn, first_op, reason="superseded")
        second_op, created = await request_draft_operation(conn, "extract_profile_text", draft_id, defer=None)
    assert created and second_op != first_op
    await op_runner.run_one(second_op)
    pause.resume()
    await asyncio.wait_for(first, 30)
    assert (await draft_row(engine, draft_id)).status == "text_review"
    graph = op_runner.deps.profile_graphs.text
    states = [
        (await graph.aget_state({"configurable": {"thread_id": thread_id("t", draft_id, op, 0)}})).values
        for op in (first_op, second_op)
    ]
    assert [(state["operation_id"], state["steps"]) for state in states] == [
        (first_op, ["parse"]),
        (second_op, ["parse", "strip_contacts"]),
    ]


async def test_the_checkpoints_of_the_thread_hold_no_cv_text(engine, op_runner, upload) -> None:
    draft_id = await upload(TWO_PAGE_PDF)
    await op_runner.run_pending()
    assert (await draft_row(engine, draft_id)).status == "text_review"
    await assert_no_cv_text_in_checkpoints(engine)


def _statement_error(text: str) -> None:
    """A failed write of CV text as SQLAlchemy raises it: the driver's message quotes the value."""
    raise sa.exc.StatementError(
        f'(psycopg.DataError) invalid input value: "{MARKER}"',
        "UPDATE cv_files SET stripped_text = %(t)s WHERE id = %(i)s",
        {"t": text, "i": 1},
        ValueError(text[:200]),
    )


def _refused_with_the_text_in_a_local(text: str) -> None:
    """An LLM error, whose text is a code, raised from a frame that holds CV text in a local (as the gateway's
    frames hold the prompt): its traceback refers to that frame."""
    prompt = f"<cv_text>{text}</cv_text>"
    assert MARKER in prompt
    raise Refused("cyber")


@pytest.mark.parametrize(
    ("raiser", "error_class"),
    [(_statement_error, b"NodeFailed"), (_refused_with_the_text_in_a_local, b"Refused")],
    ids=["statement_error", "llm_error_with_a_local"],
)
async def test_node_errors_reach_no_checkpoint_write(
    engine, op_runner, upload, monkeypatch, log_events, raiser, error_class
) -> None:
    """Deviation 25: LangGraph stores the exception that escapes a node in `checkpoint_writes` (its `repr`,
    never a traceback or a frame's locals). An SQLAlchemy error that quotes CV text is replaced by
    `NodeFailed`; an `LLMError`, whose text is a code, passes through, and its frames' locals stay in memory.
    """
    monkeypatch.setattr(strip_node, "strip_contacts", raiser)
    draft_id = await upload(TWO_PAGE_PDF)
    [op_id] = await op_runner.run_pending()
    op = await op_row(engine, op_id)
    assert (op.status, op.last_error_code) == ("pending", "bug")  # the handler raised: a requeue
    assert (await draft_row(engine, draft_id)).status == "extracting_text"
    [stored] = await error_writes(engine, thread_id("t", draft_id, op_id, 0))
    assert error_class in stored  # the scan below covers a stored exception
    await assert_no_cv_text_in_checkpoints(engine)
    rendered = json.dumps(log_events(), ensure_ascii=False)  # every line at INFO and above, as written
    assert MARKER not in rendered and "max.mustermann" not in rendered
    [failed] = events_named(log_events, "operation_handler_failed")
    assert failed["error_class"] == error_class.decode()


# --- the operation's end commits with the domain write ---------------------------------------------------


async def test_direct_failures_commit_with_the_operation_end(engine, op_runner, upload) -> None:
    """`low_text` here; Tasks 13 and 20 add `refused` and an exhausted repair. The draft's `failed(code)` and
    the operation's `failed` are one transaction (their rows' `xmin`), with no requeue."""
    draft_id = await upload(image_only_pdf())
    [op_id] = await op_runner.run_pending()
    draft, op = await draft_row(engine, draft_id), await op_row(engine, op_id)
    assert (draft.status, draft.failure_code) == ("failed", "low_text")
    assert (op.status, op.last_error_code, op.attempts) == ("failed", "low_text", 0)
    assert await draft_xmin(engine, draft_id) == op.xmin
    assert await count_rows(engine, "alerts", code="operation.failed") == 1


async def test_text_review_and_the_operation_end_commit_together(
    engine, op_runner, upload, log_events
) -> None:
    draft_id = await upload(TWO_PAGE_PDF)
    op_id = await one_op(op_runner)
    pause = PausePoint("profile.text.after_text_review_commit")
    execution = asyncio.create_task(op_runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    op = await op_row(engine, op_id)
    assert op.status == "succeeded" and op.claim_id is None  # already, right after the write's commit
    assert (await draft_row(engine, draft_id)).status == "text_review"
    assert await draft_xmin(engine, draft_id) == op.xmin
    pause.resume()
    await asyncio.wait_for(execution, 30)
    assert (await op_row(engine, op_id)).xmin == op.xmin  # nothing was written after it
    assert events_named(log_events, "operation_fenced") == []
    [finished] = events_named(log_events, "operation_finished")
    assert (finished["operation_id"], finished["status"]) == (op_id, "succeeded")


async def test_an_obsolete_operation_succeeds_without_touching_the_draft(engine, op_runner, upload) -> None:
    draft_id = await upload(TWO_PAGE_PDF)
    op_id = await one_op(op_runner)
    async with engine.begin() as conn:  # reviewed and discarded meanwhile, by another operation's path
        for before, after in (("uploaded", "extracting_text"), ("extracting_text", "text_review")):
            await transition(conn, "profile_drafts", draft_id, [before], after)
        await transition(conn, "profile_drafts", draft_id, ["text_review"], "discarded", reason="superseded")
    await op_runner.run_one(op_id)
    assert (await op_row(engine, op_id)).status == "succeeded"
    assert (await draft_row(engine, draft_id)).status == "discarded"
    assert op_runner.docparse_jobs == []


# --- the failure hook ------------------------------------------------------------------------------------


async def test_a_text_phase_exhausted_by_ten_reclaims_fails_the_draft_in_the_same_transaction(
    engine, op_runner, upload
) -> None:
    draft_id = await upload(TWO_PAGE_PDF)
    op_id = await one_op(op_runner)
    pause = PausePoint("profile.text.after_parse_commit")
    first = asyncio.create_task(op_runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    assert (await draft_row(engine, draft_id)).status == "extracting_text"
    await reclaim_stale_now(engine, op_id)  # reclaim 1
    for _ in range(ops.MAX_OPERATION_ATTEMPTS - 1):  # reclaims 2 to 10, each of a fresh claim
        async with engine.begin() as conn:
            assert await ops.claim_operation(conn, op_id, epoch=op_runner.epoch) is not None
        await reclaim_stale_now(engine, op_id)
    op = await op_row(engine, op_id)
    assert (op.status, op.attempts, op.last_error_code) == ("failed", 10, "attempts_exhausted")
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("failed", "attempts_exhausted")
    assert await draft_xmin(engine, draft_id) == op.xmin
    pause.resume()
    await asyncio.wait_for(first, 30)  # fenced: the draft stays as the hook left it
    assert (await draft_row(engine, draft_id)).status == "failed"


async def _no_handler(monkeypatch: pytest.MonkeyPatch, op_runner: OpRunner, op_id: int) -> str:
    monkeypatch.delitem(ops.HANDLERS, "extract_profile_text")
    await op_runner.run_one(op_id)
    return "no_handler"


async def _ten_bug_requeues(monkeypatch: pytest.MonkeyPatch, op_runner: OpRunner, op_id: int) -> str:
    async def broken(ctx: ops.OperationContext) -> None:
        raise RuntimeError("an unexpected error")

    monkeypatch.setitem(ops.HANDLERS, "extract_profile_text", broken)
    for _ in range(ops.MAX_OPERATION_ATTEMPTS):
        await op_runner.run_one(op_id)
        async with op_runner.engine.begin() as conn:  # due again at once, not after `RETRY_DELAY`
            await conn.execute(
                sa.text("UPDATE operations SET not_before = NULL WHERE id = :i AND status = 'pending'"),
                {"i": op_id},
            )
    return "bug"


@pytest.mark.parametrize("fail", [_no_handler, _ten_bug_requeues], ids=["no_handler", "ten_bug_requeues"])
async def test_failure_before_the_first_transition_frees_the_upload_slot(
    engine, op_runner, upload, monkeypatch, fail
) -> None:
    """F7: §7.16.8 has no `uploaded → failed` edge, and a draft left `uploaded` would answer 409 to every
    later upload. The hook takes `uploaded → extracting_text → failed(code)` in the operation's end."""
    draft_id = await upload(TWO_PAGE_PDF)
    op_id = await one_op(op_runner)
    code = await fail(monkeypatch, op_runner, op_id)
    op = await op_row(engine, op_id)
    assert op.status == "failed"
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("failed", code)
    assert await draft_xmin(engine, draft_id) == op.xmin
    await upload(PASTE)  # 202: the slot is free


# --- the operation runtime: handler-side ends and the failure hooks of every exhaustion -------------------


async def _create(engine: AsyncEngine, kind: ops.OperationKind = "export") -> int:
    async with engine.begin() as conn:
        op_id, _ = await ops.create_operation(
            conn, kind=kind, payload={"export_id": 3}, dedup_key=f"{kind}:3", defer=None
        )
    return op_id


async def test_a_handler_side_requeue_leaves_the_operation_pending_and_nothing_after_it(
    engine, current_epoch, monkeypatch, log_events
) -> None:
    not_before = utcnow() + timedelta(minutes=5)

    async def waiting(ctx: ops.OperationContext) -> None:
        async with ctx.engine.begin() as conn:
            await ops.lock_operation_owner(conn, ctx.claim)
            await ctx.requeue_in(conn, error_code="test_wait", not_before=not_before)

    monkeypatch.setitem(ops.HANDLERS, "export", waiting)
    op_id = await _create(engine)
    await ops.execute_operation(engine, op_id, epoch=current_epoch)
    op = await op_row(engine, op_id)
    assert (op.status, op.attempts, op.last_error_code, op.claim_id) == ("pending", 1, "test_wait", None)
    assert op.not_before == not_before
    assert events_named(log_events, "operation_fenced") == []
    assert events_named(log_events, "operation_finished") == []
    [requeued] = events_named(log_events, "operation_requeued")
    assert (requeued["operation_id"], requeued["status"]) == (op_id, "pending")


class RecordingHook:
    """A failure hook that records its arguments and the transaction it ran in."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, JSONValue], str | None, str]] = []

    async def __call__(
        self,
        conn: AsyncConnection,
        operation_id: int,
        payload: Mapping[str, JSONValue],
        error_code: str | None,
    ) -> None:
        xid = (await conn.execute(sa.text("SELECT (txid_current() % 4294967296)::text"))).scalar_one()
        self.calls.append((operation_id, dict(payload), error_code, str(xid)))


async def _exhaust_by_requeue(engine: AsyncEngine, op_id: int, worker_lock: Any) -> str | None:
    async with engine.begin() as conn:
        claim = await ops.claim_operation(conn, op_id, epoch=1)
        assert claim is not None
        assert (
            await ops.requeue_operation(conn, claim, error_code="transient", not_before=utcnow()) == "failed"
        )
    return "transient"


async def _exhaust_by_reclaim(engine: AsyncEngine, op_id: int, worker_lock: Any) -> str | None:
    async with engine.begin() as conn:
        assert await ops.claim_operation(conn, op_id, epoch=1) is not None
    await reclaim_stale_now(engine, op_id)
    return None


async def _exhaust_by_takeover(engine: AsyncEngine, op_id: int, worker_lock: Any) -> str | None:
    async with engine.begin() as conn:
        assert await ops.claim_operation(conn, op_id, epoch=1) is not None
    await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, worker_lock.backend_pid, "v"))
    return None


@pytest.mark.parametrize(
    "exhaust",
    [_exhaust_by_requeue, _exhaust_by_reclaim, _exhaust_by_takeover],
    ids=["requeue", "stale_reclaim", "takeover_sweep"],
)
async def test_every_exhaustion_runs_the_failure_hook_in_its_transaction(
    engine, current_epoch, worker_lock, monkeypatch, exhaust
) -> None:
    hook = RecordingHook()
    monkeypatch.setitem(ops.FAILURE_HOOKS, "export", hook)
    op_id = await _create(engine)
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE operations SET attempts = 9 WHERE id = :i"), {"i": op_id})
    code = await exhaust(engine, op_id, worker_lock)
    op = await op_row(engine, op_id)
    assert (op.status, op.last_error_code) == ("failed", "attempts_exhausted")
    assert hook.calls == [(op_id, {"export_id": 3}, code, op.xmin)]


async def test_a_failed_finish_runs_the_hook_and_a_succeeded_one_does_not(
    engine, current_epoch, monkeypatch
) -> None:
    hook = RecordingHook()
    monkeypatch.setitem(ops.FAILURE_HOOKS, "export", hook)
    failed_op = await _create(engine)
    async with engine.begin() as conn:
        claim = await ops.claim_operation(conn, failed_op, epoch=current_epoch)
        assert claim is not None
        await ops.finish_operation(conn, claim, status="failed", error_code="bad_input")
    assert hook.calls == [(failed_op, {"export_id": 3}, "bad_input", (await op_row(engine, failed_op)).xmin)]
    succeeded_op = await _create(engine)
    async with engine.begin() as conn:
        claim = await ops.claim_operation(conn, succeeded_op, epoch=current_epoch)
        assert claim is not None and claim.attempts == 0
        await ops.finish_operation(conn, claim, status="succeeded")
    assert len(hook.calls) == 1
