"""The M2 retention rules (spec §20.1, §7.4, §7.10, §7.13; plan Tasks 19 and 23).

Task 19: the `embeddings` rule keeps what `profile_vectors` references. §20.1: "Embeddings: while a revision
or profile vector references them; `retention_daily` (unreferenced rows)", and a profile's vectors live "with
the profile content", removed by the profile purge. The schema agrees: `profile_vectors.embedding_id` has no
cascade (a delete of a referenced embedding is refused), and `profile_vectors.profile_version_id` cascades
only when the version row itself is deleted, which only delete-everything does. A profile purge keeps the
version row (it nulls `profile_json`), so it deletes the version's `profile_vectors` explicitly (§20.2 step
3); their embeddings are then unreferenced and the next daily pass removes them.

Task 23: `draft_content`, `cv_files`, `cv_store_orphans`, `eval_runs` and `profile_checkpoints`, each at its
edge, and the two serialisations of the `cv_files` batch: with a retry (the draft lock) and with a
reactivation (`active_config`). The rows a rule reads are written directly where only the rule is under test
and the time is moved by aging them (`status_changed_at`, `uploaded_at`, `finished_at`, a file's mtime), so a
pass runs at the real `now` and leaves the test's session alone; the versions, the retries and the confirms
go through the real routes and the in-process runner on the invented `SYNTHETIC_CV` (a PDF of it, so each
upload stores a file in the test's CV store), on the fake LLM. No real CV and no paid call.
"""

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.routes import profile_review
from jobhunter.clock import utcnow
from jobhunter.embeddings import EMBEDDING_MODEL_ID, text_sha256, upsert_embedding
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.profile.graph import thread_id
from jobhunter.llm import fake
from jobhunter.privacy import retention
from jobhunter.privacy.retention import CV_FILES_AFTER_SELECT, run_retention_daily
from jobhunter.profile import activation
from jobhunter.profile.storage import CvStore
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import PausePoint
from tests.support.m2 import (
    insert_cv_file,
    insert_embedding,
    insert_llm_call,
    insert_profile_chain,
    insert_vector,
)
from tests.support.pg import PgServer, blocked_or_done
from tests.support.procs import await_until
from tests.support.profile import (
    SYNTHETIC_CV,
    OpRunner,
    ProfileEdit,
    Upload,
    cv_row,
    draft_row,
    idem,
    make_ready_draft,
    minimal_pdf,
)
from tests.support.settings import activate_profile, fresh_key

pytestmark = pytest.mark.integration

CURRENT_EPOCH = 1  # the worker epoch the `current_epoch` fixture records
DAY = timedelta(days=1)
# A data key of the tests' own: retention deletes the files it is given, it never opens one.
KEYS: Final = [b"\x07" * 32]
TEXT_PDF: Final = minimal_pdf(SYNTHETIC_CV.splitlines())
PURGED: Final = "The CV was deleted after 30 days; upload or paste it again"
CHECKPOINT_TABLES: Final = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


@pytest.fixture
def cv_store(cv_store_dir: Path) -> CvStore:
    """The CV store of the passes: the one the test's api and runner store their uploads in."""
    return CvStore(cv_store_dir)


async def retention_pass(
    engine: AsyncEngine, queue_app: Any, migrated: PgServer, cv_store: CvStore, *, now: datetime | None = None
) -> dict[str, int]:
    """One `retention_daily` pass at `now` (by default the real one)."""
    async with open_saver(migrated.dsn("app")) as saver:
        return await run_retention_daily(
            engine, queue_app, saver, cv_store=cv_store, now=now or utcnow(), epoch=CURRENT_EPOCH
        )


async def embedding_ids(engine: AsyncEngine) -> set[int]:
    async with engine.connect() as c:
        return set((await c.execute(sa.text("SELECT id FROM embeddings"))).scalars())


async def test_embeddings_referenced_by_profile_vectors_are_kept(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    async with engine.begin() as c:
        first = await insert_profile_chain(c, 1)
        second = await insert_profile_chain(c, 2)
        # both versions hold a `summary` vector: two embedding rows, one each (the helper hashes both)
        referenced = {
            await insert_vector(c, first.profile_version_id, "summary"),
            await insert_vector(c, first.profile_version_id, "title:0"),
            await insert_vector(c, second.profile_version_id, "summary"),
        }
        unreferenced = {await insert_embedding(c, "loose-1"), await insert_embedding(c, "loose-2")}
    assert len(referenced) == 3
    assert await embedding_ids(engine) == referenced | unreferenced

    report = await retention_pass(engine, queue_app, migrated, cv_store, now=utcnow() + DAY)

    assert report["embeddings"] == 2
    assert await embedding_ids(engine) == referenced


async def test_the_embeddings_of_a_purged_versions_deleted_vectors_go_with_the_next_pass(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    async with engine.begin() as c:
        purged = await insert_profile_chain(c, 1)
        kept = await insert_profile_chain(c, 2)
        gone = {await insert_vector(c, purged.profile_version_id, k) for k in ("summary", "title:0")}
        stays = {await insert_vector(c, kept.profile_version_id, "summary")}
    later = utcnow() + DAY
    assert (await retention_pass(engine, queue_app, migrated, cv_store, now=later))["embeddings"] == 0

    async with engine.begin() as c:  # §20.2 step 3: the purge deletes the version's vectors
        await c.execute(
            sa.text("DELETE FROM profile_vectors WHERE profile_version_id = :p"),
            {"p": purged.profile_version_id},
        )

    later = utcnow() + DAY
    assert (await retention_pass(engine, queue_app, migrated, cv_store, now=later))["embeddings"] == len(gone)
    assert await embedding_ids(engine) == stays


async def test_an_embedding_a_profile_write_is_about_to_reference_survives_the_pass(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    """`embed_profile` upserts an embedding row and then references it from `profile_vectors` in one
    transaction. An existing, unreferenced row it has upserted is held `FOR KEY SHARE`
    (`upsert_embedding`), so a pass that runs in between skips it instead of deleting a row the write is
    about to reference."""
    text = "Senior Backend Developer"
    async with engine.begin() as c:
        chain = await insert_profile_chain(c, 1)
        left_over = await upsert_embedding(
            c,
            model_id=EMBEDDING_MODEL_ID,
            text_sha256=text_sha256(text),
            input_kind="query",
            vector=[0.0] * 1023 + [1.0],
        )
    async with engine.connect() as writer:
        transaction = await writer.begin()
        again = await upsert_embedding(
            writer,
            model_id=EMBEDDING_MODEL_ID,
            text_sha256=text_sha256(text),
            input_kind="query",
            vector=[0.0] * 1023 + [1.0],
        )
        assert again == left_over  # the existing row, now held

        later = utcnow() + DAY
        assert (await retention_pass(engine, queue_app, migrated, cv_store, now=later))["embeddings"] == 0

        await writer.execute(
            sa.text(
                "INSERT INTO profile_vectors (profile_version_id, kind, embedding_id) "
                "VALUES (:p, 'summary', :e)"
            ),
            {"p": chain.profile_version_id, "e": again},
        )
        await transaction.commit()

    later = utcnow() + DAY
    assert (await retention_pass(engine, queue_app, migrated, cv_store, now=later))["embeddings"] == 0
    assert await embedding_ids(engine) == {left_over}


# --- Task 23: rows written directly, aged into their windows ---------------------------------------------

_INSERT_DRAFT = sa.text(
    "INSERT INTO profile_drafts (cv_file_id, status, status_changed_at, failure_code, reviewed_text, "
    "reviewed_text_sha256, draft_json) VALUES (:cv, :status, now() - make_interval(days => :days), :code, "
    "'Synthetischer Lebenslauf', '\\x01', CAST('{\"summary\": \"synthetisch\"}' AS jsonb)) RETURNING id"
)
_AGE_DRAFT = sa.text(
    "UPDATE profile_drafts SET status_changed_at = now() - make_interval(days => :days) WHERE id = :d"
)
_SET_CONTENT = sa.text(
    "UPDATE profile_drafts SET reviewed_text = 'Synthetischer Lebenslauf', "
    'draft_json = CAST(\'{"summary": "synthetisch"}\' AS jsonb) WHERE id = :d'
)
_SET_OUTPUT = sa.text(
    'UPDATE llm_calls SET output_json = CAST(\'{"summary": "synthetisch"}\' AS jsonb) WHERE id = :i'
)
_INSERT_FILE = sa.text(
    "INSERT INTO cv_files (sha256, mime, size_bytes, storage_path, raw_text_enc, stripped_text, uploaded_at) "
    "VALUES (:h, 'application/pdf', 100, :path, '\\x00', 'Synthetischer Text', "
    "now() - make_interval(days => :days)) RETURNING id"
)


async def insert_draft(c: Any, cv_file_id: int, status: str, *, days: int, code: str | None = None) -> int:
    """A draft of `cv_file_id` in `status` since `days` ago, with content (`failed` needs a `code`)."""
    params = {"cv": cv_file_id, "status": status, "days": days, "code": code}
    return int((await c.execute(_INSERT_DRAFT, params)).scalar_one())


async def stored_file(c: Any, cv_store: CvStore, n: int, *, uploaded_days: int = 0) -> tuple[int, str]:
    """A `cv_files` row with a real file in `cv_store`, its texts set, uploaded `uploaded_days` ago:
    `(id, storage_path)`."""
    stored = cv_store.write(b"%PDF-1.4 synthetic " + str(n).encode(), KEYS)
    params = {"h": b"t23-" + n.to_bytes(4, "big"), "path": stored.storage_path, "days": uploaded_days}
    return int((await c.execute(_INSERT_FILE, params)).scalar_one()), stored.storage_path


async def draft_content(engine: AsyncEngine, draft_id: int) -> tuple[object, ...]:
    """`(draft_json is set, reviewed_text is set, content_purged_at is set, reviewed_text_sha256 is set)`."""
    async with engine.connect() as c:
        row = await c.execute(
            sa.text(
                "SELECT draft_json IS NOT NULL, reviewed_text IS NOT NULL, content_purged_at IS NOT NULL, "
                "reviewed_text_sha256 IS NOT NULL FROM profile_drafts WHERE id = :d"
            ),
            {"d": draft_id},
        )
        return tuple(row.one())


async def has_output(engine: AsyncEngine, call_id: int) -> bool:
    async with engine.connect() as c:
        statement = sa.text("SELECT output_json IS NOT NULL FROM llm_calls WHERE id = :i")
        return bool((await c.execute(statement, {"i": call_id})).scalar_one())


async def file_row(engine: AsyncEngine, cv_file_id: int) -> tuple[object, ...]:
    """`(storage_path, raw_text_enc is set, stripped_text is set, purged_at is set)` of a `cv_files` row."""
    async with engine.connect() as c:
        row = await c.execute(
            sa.text(
                "SELECT storage_path, raw_text_enc IS NOT NULL, stripped_text IS NOT NULL, "
                "purged_at IS NOT NULL FROM cv_files WHERE id = :i"
            ),
            {"i": cv_file_id},
        )
        return tuple(row.one())


KEPT_TEXTS: Final = (True, True, False)  # texts set, not purged (after the storage path)
PURGED_ROW: Final = (None, False, False, True)


async def test_draft_content_goes_30_days_after_the_draft_ended(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    async with engine.begin() as c:
        old_chain = await insert_profile_chain(c, 1)
        old_confirmed = old_chain.draft_id
        young_confirmed = (await insert_profile_chain(c, 2)).draft_id
        cv = [await insert_cv_file(c, n) for n in (3, 4, 5, 6)]
        old_discarded = await insert_draft(c, cv[0], "discarded", days=31)
        old_failed = await insert_draft(c, cv[1], "failed", days=31, code="refused")
        young_failed = await insert_draft(c, cv[2], "failed", days=29, code="refused")
        open_ready = await insert_draft(c, cv[3], "ready", days=40)
        for draft_id, days in ((old_confirmed, 31), (young_confirmed, 29)):
            await c.execute(_SET_CONTENT, {"d": draft_id})
            await c.execute(_AGE_DRAFT, {"d": draft_id, "days": days})
        drafts = (old_confirmed, young_confirmed, old_discarded, old_failed, young_failed, open_ready)
        calls = {}
        for draft_id in drafts:  # each draft's extraction call, with its output
            _, calls[draft_id] = await insert_llm_call(c, draft_id=draft_id)
            await c.execute(_SET_OUTPUT, {"i": calls[draft_id]})
        _, proposal_call = await insert_llm_call(
            c, purpose="propose_queries"
        )  # no draft's: never this rule's
        await c.execute(_SET_OUTPUT, {"i": proposal_call})

    report = await retention_pass(engine, queue_app, migrated, cv_store)

    assert report["draft_content"] == 3
    for draft_id in (old_confirmed, old_discarded, old_failed):
        assert await draft_content(engine, draft_id) == (False, False, True, True)  # the hash stays
        assert not await has_output(engine, calls[draft_id])
    for draft_id in (young_confirmed, young_failed, open_ready):
        assert await draft_content(engine, draft_id) == (True, True, False, True)
        assert await has_output(engine, calls[draft_id])
    assert await has_output(engine, proposal_call)
    confirmed = await draft_row(engine, old_confirmed)  # its state and stored confirm response stay
    assert (confirmed.status, confirmed.confirm_response) == (
        "confirmed",
        {"profile_version_id": old_chain.profile_version_id},
    )
    assert (await retention_pass(engine, queue_app, migrated, cv_store))["draft_content"] == 0


async def test_the_content_purged_at_checks_hold(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    """A `ready` or `confirmed` draft loses its profile only with `content_purged_at` set (the §7.4 CHECK):
    the rule's rows satisfy it, and a write that nulls the profile without it is refused."""
    async with engine.begin() as c:
        purged = (await insert_profile_chain(c, 1)).draft_id
        unpurged = (await insert_profile_chain(c, 2)).draft_id
        await c.execute(_AGE_DRAFT, {"d": purged, "days": 31})
        ready = await insert_draft(c, await insert_cv_file(c, 3), "ready", days=40)
    assert (await retention_pass(engine, queue_app, migrated, cv_store))["draft_content"] == 1
    assert (await draft_content(engine, purged))[:3] == (
        False,
        False,
        True,
    )  # a confirmed row the CHECK allows
    for draft_id in (unpurged, ready):
        with pytest.raises(IntegrityError, match="profile_drafts_check"):
            async with engine.begin() as c:
                await c.execute(
                    sa.text("UPDATE profile_drafts SET draft_json = NULL WHERE id = :d"), {"d": draft_id}
                )


async def test_cv_files_of_ended_drafts_and_abandoned_uploads_go_after_30_days(
    engine, current_epoch, queue_app, migrated, cv_store, cv_store_dir
) -> None:
    async with engine.begin() as c:
        discarded_31, discarded_31_path = await stored_file(c, cv_store, 1)
        discarded_29, discarded_29_path = await stored_file(c, cv_store, 2)
        failed_31, failed_31_path = await stored_file(c, cv_store, 3)
        abandoned_31, abandoned_31_path = await stored_file(c, cv_store, 4, uploaded_days=31)
        abandoned_29, abandoned_29_path = await stored_file(c, cv_store, 5, uploaded_days=29)
        open_old, open_old_path = await stored_file(c, cv_store, 6, uploaded_days=40)
        gone_31, gone_31_path = await stored_file(c, cv_store, 7)
        pasted_31 = await insert_cv_file(c, 8)  # a pasted text: no file, its texts only
        await c.execute(
            sa.text("UPDATE cv_files SET raw_text_enc = '\\x00', stripped_text = 'Text' WHERE id = :i"),
            {"i": pasted_31},
        )
        await insert_draft(c, discarded_31, "discarded", days=31)
        await insert_draft(c, discarded_29, "discarded", days=29)
        await insert_draft(c, failed_31, "failed", days=31, code="low_text")
        await insert_draft(c, open_old, "ready", days=40)
        await insert_draft(c, gone_31, "failed", days=31, code="low_text")
        await insert_draft(c, pasted_31, "discarded", days=31)
    cv_store.delete(gone_31_path)  # already gone from the store: purging its row is still fine

    report = await retention_pass(engine, queue_app, migrated, cv_store)

    assert report["cv_files"] == 5
    for cv_file_id, path in (
        (discarded_31, discarded_31_path),
        (failed_31, failed_31_path),
        (abandoned_31, abandoned_31_path),
        (gone_31, gone_31_path),
    ):
        assert await file_row(engine, cv_file_id) == PURGED_ROW
        assert not (cv_store_dir / path).exists()
    assert await file_row(engine, pasted_31) == PURGED_ROW
    for cv_file_id, path in (
        (discarded_29, discarded_29_path),
        (abandoned_29, abandoned_29_path),
        (open_old, open_old_path),
    ):
        assert await file_row(engine, cv_file_id) == (path, *KEPT_TEXTS)
        assert (cv_store_dir / path).is_file()
    assert (await retention_pass(engine, queue_app, migrated, cv_store))["cv_files"] == 0


async def test_cv_store_orphans_go_a_day_after_they_were_written(
    engine, current_epoch, queue_app, migrated, cv_store, cv_store_dir
) -> None:
    """A file no `cv_files` row names (an upload whose transaction rolled back after the write) goes once it
    is a day old; a named file, a younger one and anything that is not the store's stay."""
    named = cv_store.write(b"named", KEYS).storage_path
    async with engine.begin() as c:  # a fresh upload's row: neither old nor ended
        await c.execute(_INSERT_FILE, {"h": b"t23-named", "path": named, "days": 0})
    orphan_old = cv_store.write(b"orphan, old", KEYS).storage_path
    orphan_young = cv_store.write(b"orphan, young", KEYS).storage_path
    foreign = cv_store_dir / "notes.txt"
    foreign.write_text("not the store's")
    now = utcnow()
    for path, age in (
        (named, timedelta(hours=25)),
        (orphan_old, timedelta(hours=25)),
        (orphan_young, timedelta(hours=23)),
        ("notes.txt", timedelta(days=30)),
    ):
        stamp = (now - age).timestamp()
        os.utime(cv_store_dir / path, (stamp, stamp))

    report = await retention_pass(engine, queue_app, migrated, cv_store, now=now)

    assert report["cv_store_orphans"] == 1
    assert sorted(p.name for p in cv_store_dir.iterdir()) == sorted([named, orphan_young, "notes.txt"])


_INSERT_EVAL_RUN = sa.text(
    "INSERT INTO eval_runs (kind, route, status, started_at, finished_at) "
    "VALUES (:kind, 'production', :status, :started, :finished) RETURNING id"
)


async def test_eval_runs_go_after_13_months_but_the_latest_completed_of_each_kind(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    now = utcnow()

    async def run(c: Any, kind: str, status: str, finished_days: int | None, started_days: int = 600) -> int:
        finished = None if finished_days is None else now - timedelta(days=finished_days)
        params = {
            "kind": kind,
            "status": status,
            "started": now - timedelta(days=started_days),
            "finished": finished,
        }
        return int((await c.execute(_INSERT_EVAL_RUN, params)).scalar_one())

    async with engine.begin() as c:
        old_completed = await run(c, "schema_dry_run", "completed", 500)
        latest_completed = await run(c, "schema_dry_run", "completed", 425)  # about 14 months, and the latest
        failed_newer = await run(c, "schema_dry_run", "failed", 420)
        failed_398 = await run(c, "schema_dry_run", "failed", 398)
        failed_396 = await run(c, "schema_dry_run", "failed", 396)
        canary_only = await run(c, "canary", "completed", 500)
        golden_running = await run(c, "golden", "running", None)
        ledger, _ = await insert_llm_call(c, purpose="schema_dry_run", eval_run_id=old_completed)
        _, kept_call = await insert_llm_call(c, purpose="schema_dry_run", eval_run_id=latest_completed)

    report = await retention_pass(engine, queue_app, migrated, cv_store, now=now)

    assert report["eval_runs"] == 3
    async with engine.connect() as c:
        left = set((await c.execute(sa.text("SELECT id FROM eval_runs"))).scalars())
        calls = set((await c.execute(sa.text("SELECT id FROM llm_calls"))).scalars())
        ledgers = set((await c.execute(sa.text("SELECT id FROM llm_spend_ledger"))).scalars())
    assert left == {latest_completed, failed_396, canary_only, golden_running}
    assert old_completed not in left and failed_newer not in left and failed_398 not in left
    assert calls == {kept_call}  # the deleted run's call went with it (cascade)
    assert ledger in ledgers  # the spend ledger is kept indefinitely


# --- Task 23: profile_checkpoints -------------------------------------------------------------------------

_INSERT_OPERATION = sa.text(
    "INSERT INTO operations (kind, payload, dedup_key, status, last_error_code, created_at, finished_at) "
    "VALUES (:kind, '{}', :key, :status, :code, now() - interval '40 days', "
    "CASE WHEN CAST(:finished_days AS integer) IS NULL THEN NULL "
    "ELSE now() - make_interval(days => CAST(:finished_days AS integer)) END) RETURNING id"
)
_THREAD_ROWS = (
    sa.text("INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint) VALUES (:t, '1', '{}')"),
    sa.text(
        "INSERT INTO checkpoint_blobs (thread_id, channel, version, type) VALUES (:t, 'steps', '1', 'empty')"
    ),
    sa.text(
        "INSERT INTO checkpoint_writes (thread_id, checkpoint_id, task_id, idx, channel, blob) "
        "VALUES (:t, '1', 'task', 0, 'steps', '\\x00')"
    ),
)


async def insert_thread(c: Any, thread: str) -> None:
    """A thread with a row in each of the saver's three tables."""
    for statement in _THREAD_ROWS:
        await c.execute(statement, {"t": thread})


async def thread_rows(engine: AsyncEngine, thread: str) -> list[int]:
    """The rows of `thread` in `checkpoints`, `checkpoint_blobs` and `checkpoint_writes`."""
    async with engine.connect() as c:
        return [
            int(
                (
                    await c.execute(
                        sa.text(f"SELECT count(*) FROM {table} WHERE thread_id = :t"),  # noqa: S608
                        {"t": thread},
                    )
                ).scalar_one()
            )
            for table in CHECKPOINT_TABLES
        ]


async def test_profile_threads_go_14_days_after_their_operation_ended_or_with_it(
    engine, current_epoch, queue_app, migrated, cv_store
) -> None:
    async with engine.begin() as c:

        async def operation(
            kind: str, status: str, finished_days: int | None, code: str | None = None
        ) -> int:
            params = {
                "kind": kind,
                "key": fresh_key(kind),
                "status": status,
                "code": code,
                "finished_days": finished_days,
            }
            return int((await c.execute(_INSERT_OPERATION, params)).scalar_one())

        ended_15 = await operation("extract_profile_text", "succeeded", 15)
        ended_13 = await operation("extract_profile", "failed", 13, code="refused")
        proposal_15 = await operation("propose_queries", "succeeded", 15)
        live = await operation("propose_queries", "pending", None)
        gone = thread_id("p", 41, 9_000_000, 2)  # its operation no longer exists (the `operations` rule)
        threads = {
            "ended_15": thread_id("t", 40, ended_15, 0),
            "ended_13": thread_id("p", 40, ended_13, 1),
            "proposal_15": thread_id("q", 7, proposal_15, 0),
            "live": thread_id("q", 8, live, 3),
            "gone": gone,
            "unattributed": "profile-40-x1-a0",  # no name the profile graphs write
            "run": "run-424242-a1",  # a run thread without a run: neither rule's
        }
        for thread in threads.values():
            await insert_thread(c, thread)

    report = await retention_pass(engine, queue_app, migrated, cv_store)

    assert report["profile_checkpoints"] == 3
    for name in ("ended_15", "proposal_15", "gone"):  # with their checkpoints, blobs and writes
        assert await thread_rows(engine, threads[name]) == [0, 0, 0], name
    for name in ("ended_13", "live", "unattributed", "run"):
        assert await thread_rows(engine, threads[name]) == [1, 1, 1], name
    assert (await retention_pass(engine, queue_app, migrated, cv_store))["profile_checkpoints"] == 0


async def test_a_finished_proposals_thread_goes_while_a_running_one_of_the_same_version_stays(
    engine, current_epoch, queue_app, migrated, cv_store, logged_in_client, op_runner: OpRunner
) -> None:
    """Deviation 27: the thread of each execution carries its own operation, so a new proposal of the same
    profile version, still running, keeps its thread while the finished one's goes."""
    await activate_profile(engine)

    async def propose() -> tuple[int, int]:
        r = await logged_in_client.post("/api/profile/query-proposals", headers=idem(fresh_key("proposal")))
        assert r.status_code == 202, r.text
        proposal_id = int(r.json()["proposal_id"])
        async with engine.connect() as c:
            op_id = await c.execute(
                sa.text(
                    "SELECT id FROM operations WHERE kind = 'propose_queries' "
                    "AND payload ->> 'proposal_id' = :p"
                ),
                {"p": str(proposal_id)},
            )
            return proposal_id, int(op_id.scalar_one())

    first, first_op = await propose()
    await op_runner.run_pending()
    finished = thread_id("q", first, first_op, 0)
    assert (await thread_rows(engine, finished))[0] > 0
    async with engine.begin() as c:
        await c.execute(
            sa.text("UPDATE operations SET finished_at = now() - interval '15 days' WHERE id = :i"),
            {"i": first_op},
        )
    second, second_op = await propose()
    pause = PausePoint("queries.propose.after_call")
    running = asyncio.create_task(op_runner.run_one(second_op, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 30)
    live = thread_id("q", second, second_op, 0)
    assert (await thread_rows(engine, live))[0] > 0

    report = await retention_pass(engine, queue_app, migrated, cv_store)

    assert report["profile_checkpoints"] == 1
    assert await thread_rows(engine, finished) == [0, 0, 0]
    assert (await thread_rows(engine, live))[0] > 0
    pause.resume()
    await asyncio.wait_for(running, 30)
    async with engine.connect() as c:
        status = await c.execute(sa.text("SELECT status FROM query_proposals WHERE id = :p"), {"p": second})
        assert status.scalar_one() == "ready"


# --- Task 23: cv_files through the real confirms ----------------------------------------------------------


def with_seniority(level: str) -> ProfileEdit:
    def edit(draft: dict[str, Any]) -> dict[str, Any]:
        return {**draft, "seniority": level}

    return edit


async def confirm(client: httpx.AsyncClient, draft_id: int) -> int:
    """`POST …/confirm` (201 required): the version it activated."""
    r = await client.post(
        f"/api/profile/drafts/{draft_id}/confirm",
        json={"expected_rescore_ack": True},
        headers=idem(fresh_key("confirm")),
    )
    assert r.status_code == 201, (r.status_code, r.text)
    return int(r.json()["profile_version_id"])


async def pdf_draft(
    client: httpx.AsyncClient, op_runner: OpRunner, upload: Upload, edit: ProfileEdit | None = None
) -> tuple[int, str]:
    """A `ready` draft of `TEXT_PDF` (its profile edited by `edit`): `(draft_id, its file's storage path)`."""
    draft_id = await make_ready_draft(client, op_runner, upload, text=TEXT_PDF, edit=edit)
    path = (await cv_row(op_runner.engine, draft_id)).storage_path
    assert path is not None
    return draft_id, str(path)


async def three_versions(
    client: httpx.AsyncClient, op_runner: OpRunner, upload: Upload
) -> tuple[list[int], list[str]]:
    """V1 (the synthetic profile), V2 (junior) and V3 (lead) confirmed in that order: their ids and files."""
    versions, paths = [], []
    for edit in (None, with_seniority("junior"), with_seniority("lead")):
        draft_id, path = await pdf_draft(client, op_runner, upload, edit)
        versions.append(await confirm(client, draft_id))
        paths.append(path)
    assert len(set(versions)) == 3
    return versions, paths


async def test_the_active_and_the_previous_versions_files_stay_and_an_older_ones_goes(
    engine, current_epoch, queue_app, migrated, cv_store, cv_store_dir, logged_in_client, op_runner, upload
) -> None:
    _, (v1_path, v2_path, v3_path) = await three_versions(logged_in_client, op_runner, upload)

    report = await retention_pass(engine, queue_app, migrated, cv_store)

    assert report["cv_files"] == 1  # V1's: neither the active version (V3) nor the previous one (V2)
    assert not (cv_store_dir / v1_path).exists()
    async with engine.connect() as c:
        row = await c.execute(
            sa.text(
                "SELECT storage_path, raw_text_enc, stripped_text, purged_at IS NOT NULL FROM cv_files "
                "WHERE storage_path IS NULL AND purged_at IS NOT NULL"
            )
        )
        assert [tuple(r) for r in row] == [(None, None, None, True)]
    assert (cv_store_dir / v2_path).is_file() and (cv_store_dir / v3_path).is_file()


async def test_an_upload_whose_confirm_reused_the_active_version_is_purged_at_once(
    engine, current_epoch, queue_app, migrated, cv_store, cv_store_dir, logged_in_client, op_runner, upload
) -> None:
    first, first_path = await pdf_draft(logged_in_client, op_runner, upload)
    version = await confirm(logged_in_client, first)
    second, second_path = await pdf_draft(logged_in_client, op_runner, upload)  # identical content
    assert await confirm(logged_in_client, second) == version  # the same `profile_hash`: reused
    assert first_path != second_path

    report = await retention_pass(engine, queue_app, migrated, cv_store)

    assert report["cv_files"] == 1
    assert not (cv_store_dir / second_path).exists()
    assert (await cv_row(engine, second)).purged_at is not None
    assert (cv_store_dir / first_path).is_file()  # the version keeps its own file
    async with engine.connect() as c:
        own = await c.execute(
            sa.text("SELECT cv_file_id FROM profile_versions WHERE id = :v"), {"v": version}
        )
        assert own.scalar_one() == (await cv_row(engine, first)).id


async def test_the_previous_version_follows_the_activation_order_not_the_version_number(
    engine, current_epoch, queue_app, migrated, cv_store, cv_store_dir, logged_in_client, op_runner, upload
) -> None:
    """V1, V2 and V3, then V1 reactivated (it replaced V3), then V2 reactivated (it replaced V1): the
    previous version of V2 is V1, the one it replaced, not V3, the highest `version_no` but V2's. So V2's and
    V1's files stay, and V3's goes with the two reusing uploads."""
    (v1, v2, _), (v1_path, v2_path, v3_path) = await three_versions(logged_in_client, op_runner, upload)
    again_v1, again_v1_path = await pdf_draft(logged_in_client, op_runner, upload)
    assert await confirm(logged_in_client, again_v1) == v1
    again_v2, again_v2_path = await pdf_draft(logged_in_client, op_runner, upload, with_seniority("junior"))
    assert await confirm(logged_in_client, again_v2) == v2

    report = await retention_pass(engine, queue_app, migrated, cv_store)

    assert report["cv_files"] == 3
    assert (cv_store_dir / v2_path).is_file() and (cv_store_dir / v1_path).is_file()
    for gone in (v3_path, again_v1_path, again_v2_path):
        assert not (cv_store_dir / gone).exists()


@pytest.mark.parametrize("order", ["confirm_first", "batch_first"])
async def test_retention_and_reactivation_are_serialised(
    engine,
    current_epoch,
    queue_app,
    migrated,
    cv_store,
    cv_store_dir,
    logged_in_client,
    op_runner,
    upload,
    monkeypatch,
    order: str,
) -> None:
    """After V1, V2 and V3, a confirm reusing V1 makes V1 active and V3 the previous version.

    `confirm_first`: the confirm commits before the batch starts, so the pass keeps V1's and V3's files and
    purges V2's and the reusing upload's. `batch_first`: a batch paused after its select holds V1 as a
    candidate and `active_config` `FOR SHARE`; the confirm waits for that lock, the batch deletes V1's file
    and commits, and only then does the confirm commit: no file is deleted after it (the pass ends with that
    batch), and V2's and V3's files stay until a pass under the new protection set."""
    (v1, _, v3), (v1_path, v2_path, v3_path) = await three_versions(logged_in_client, op_runner, upload)
    again, again_path = await pdf_draft(logged_in_client, op_runner, upload)  # V1's content again
    events: list[str] = []
    real_delete = cv_store.delete

    def recorded_delete(storage_path: str) -> None:
        events.append(f"unlink {storage_path}")
        real_delete(storage_path)

    async def marked(point: str) -> None:
        if point == activation.BEFORE_COMMIT_POINT:
            events.append("confirm before commit")

    monkeypatch.setattr(cv_store, "delete", recorded_delete)
    monkeypatch.setattr(activation, "afault_point", marked)

    if order == "confirm_first":
        assert await confirm(logged_in_client, again) == v1
        report = await retention_pass(engine, queue_app, migrated, cv_store)
        assert report["cv_files"] == 2
        assert sorted(events[1:]) == sorted([f"unlink {v2_path}", f"unlink {again_path}"])
        assert (cv_store_dir / v1_path).is_file() and (cv_store_dir / v3_path).is_file()
        return

    pause = PausePoint(CV_FILES_AFTER_SELECT)
    monkeypatch.setattr(retention, "afault_point", pause)
    batch = asyncio.create_task(retention_pass(engine, queue_app, migrated, cv_store))
    await asyncio.wait_for(pause.reached.wait(), 30)
    reactivation = asyncio.create_task(confirm(logged_in_client, again))
    await await_until(lambda: blocked_or_done(migrated, reactivation), 10, 0.02)
    assert not reactivation.done()  # waiting for `active_config`, which the batch holds `FOR SHARE`
    assert (await draft_row(engine, again)).status == "ready"
    pause.resume()
    report = await asyncio.wait_for(batch, 30)
    assert await asyncio.wait_for(reactivation, 30) == v1
    assert report["cv_files"] == 1
    assert events == [f"unlink {v1_path}", "confirm before commit"]  # nothing deleted after the confirm
    async with engine.connect() as c:
        active = await c.execute(sa.text("SELECT profile_version_id FROM active_config"))
        assert active.scalar_one() == v1
    assert (cv_store_dir / v2_path).is_file() and (cv_store_dir / v3_path).is_file()
    assert (cv_store_dir / again_path).is_file()
    assert v3 != v1


# --- Task 23: cv_files and draft_content against a retry (the draft lock) ----------------------------------


async def refused_native_draft(engine: AsyncEngine, op_runner: OpRunner, upload: Upload) -> tuple[int, str]:
    """A native-PDF draft the model refused (`failed('refused')`), ended 31 days ago: `(draft_id, its
    file)`."""
    fake.count_tokens(1800)
    fake.scenario("refusal")
    draft_id = await upload(TEXT_PDF, native_pdf_opt_in=True)
    await op_runner.run_pending()
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code, draft.native_pdf) == ("failed", "refused", True)
    fake.scenario("ok")
    async with engine.begin() as c:
        await c.execute(_AGE_DRAFT, {"d": draft_id, "days": 31})
    path = (await cv_row(engine, draft_id)).storage_path
    assert path is not None
    return draft_id, str(path)


async def retry(client: httpx.AsyncClient, draft_id: int) -> httpx.Response:
    return await client.post(
        f"/api/profile/drafts/{draft_id}/retry", json={"mode": "llm"}, headers=idem(fresh_key("retry"))
    )


async def extract_operations(engine: AsyncEngine) -> int:
    async with engine.connect() as c:
        count = await c.execute(sa.text("SELECT count(*) FROM operations WHERE kind = 'extract_profile'"))
        return int(count.scalar_one())


_PURGE_CONTENT = sa.text(
    "UPDATE profile_drafts SET draft_json = NULL, reviewed_text = NULL, "
    "content_purged_at = now() - interval '1 day' WHERE id = :d"
)


@pytest.mark.parametrize("order", ["retry_first", "retry_first_content_gone", "batch_first"])
async def test_retention_and_retry_are_serialised(
    engine,
    current_epoch,
    queue_app,
    migrated,
    cv_store,
    cv_store_dir,
    logged_in_client,
    op_runner,
    upload,
    monkeypatch,
    order: str,
) -> None:
    """A native-PDF draft `failed('refused')` 31 days ago and a retry (`mode="llm"`: the PDF again).

    `retry_first`: the retry holds the draft (`FOR UPDATE`, already `extracting_profile`) while the pass
    starts; the `draft_content` batch, which read the draft as a candidate, waits for that lock and, once the
    retry has committed, finds it reopened under the lock and skips it; the file batch then sees an open
    draft. `retry_first_content_gone`: the same with the content purged before (so the `cv_files` batch is
    the one that waits, and skips the file under the lock). `batch_first`: a `cv_files` batch paused after
    its select holds the draft `FOR UPDATE`; the retry waits for that lock, and once the batch has deleted the
    file and committed, the retry finds the PDF purged: 409 `conflict_state`, and no operation is created."""
    draft_id, path = await refused_native_draft(engine, op_runner, upload)
    operations = await extract_operations(engine)

    if order != "batch_first":
        if order == "retry_first_content_gone":
            async with engine.begin() as c:
                await c.execute(_PURGE_CONTENT, {"d": draft_id})
        held, release = asyncio.Event(), asyncio.Event()
        real_extraction = profile_review._new_extraction

        async def extraction_held(conn: Any, held_draft_id: int) -> int:
            held.set()  # the draft is locked and moved: the retry's transaction is open
            await release.wait()
            return await real_extraction(conn, held_draft_id)

        monkeypatch.setattr(profile_review, "_new_extraction", extraction_held)
        retried = asyncio.create_task(retry(logged_in_client, draft_id))
        await asyncio.wait_for(held.wait(), 30)
        batch = asyncio.create_task(retention_pass(engine, queue_app, migrated, cv_store))
        await await_until(lambda: blocked_or_done(migrated, batch), 10, 0.02)
        assert not batch.done()  # a batch waits for the draft the retry holds
        release.set()
        assert (await asyncio.wait_for(retried, 30)).status_code == 202
        report = await asyncio.wait_for(batch, 30)
        assert (report["draft_content"], report["cv_files"]) == (0, 0)
        draft = await draft_row(engine, draft_id)
        assert (draft.status, draft.content_purged_at) == ("extracting_profile", None)
        assert (cv_store_dir / path).is_file() and (await cv_row(engine, draft_id)).purged_at is None
        assert await extract_operations(engine) == operations + 1
        return

    pause = PausePoint(CV_FILES_AFTER_SELECT)
    monkeypatch.setattr(retention, "afault_point", pause)
    batch = asyncio.create_task(retention_pass(engine, queue_app, migrated, cv_store))
    await asyncio.wait_for(pause.reached.wait(), 30)
    retried = asyncio.create_task(retry(logged_in_client, draft_id))
    await await_until(lambda: blocked_or_done(migrated, retried), 10, 0.02)
    assert not retried.done()  # waiting for the draft, which the batch holds `FOR UPDATE`
    pause.resume()
    report = await asyncio.wait_for(batch, 30)
    r = await asyncio.wait_for(retried, 30)
    assert (report["draft_content"], report["cv_files"]) == (1, 1)
    assert (r.status_code, r.json()["code"], r.json()["detail"]) == (409, "conflict_state", PURGED)
    assert await extract_operations(engine) == operations
    assert (await draft_row(engine, draft_id)).status == "failed"
    assert not (cv_store_dir / path).exists() and (await cv_row(engine, draft_id)).purged_at is not None


async def test_regenerated_content_is_purged_again(
    engine,
    current_epoch,
    queue_app,
    migrated,
    cv_store,
    cv_store_dir,
    logged_in_client,
    op_runner,
    upload,
    monkeypatch,
) -> None:
    """The `draft_content` batch purges a native draft failed 31 days ago; a native retry commits before the
    file batch runs (it reopens the draft and clears `content_purged_at`), so the file stays. The retried
    extraction succeeds, the draft is discarded, and 30 days later the new `draft_json` and the new call's
    `output_json` go, and the file with them."""
    draft_id, path = await refused_native_draft(engine, op_runner, upload)
    real_apply = retention._apply

    async def retry_before_the_file_batch(rule: Any, *args: Any, **kwargs: Any) -> int:
        if rule.name == "cv_files":
            assert (await draft_content(engine, draft_id))[2]  # the content batch has committed
            assert (await retry(logged_in_client, draft_id)).status_code == 202
        return await real_apply(rule, *args, **kwargs)

    monkeypatch.setattr(retention, "_apply", retry_before_the_file_batch)
    report = await retention_pass(engine, queue_app, migrated, cv_store)
    monkeypatch.setattr(retention, "_apply", real_apply)
    assert (report["draft_content"], report["cv_files"]) == (1, 0)
    assert (await draft_row(engine, draft_id)).content_purged_at is None  # the retry cleared it
    assert (cv_store_dir / path).is_file()

    fake.count_tokens(1800)
    await op_runner.run_pending()  # the native extraction, now on the `ok` stream
    draft = await draft_row(engine, draft_id)
    assert draft.status == "ready" and draft.draft_json is not None
    async with engine.connect() as c:
        new_call = await c.execute(
            sa.text(
                "SELECT id FROM llm_calls WHERE profile_draft_id = :d AND purpose = 'extract_profile' "
                "AND output_json IS NOT NULL"
            ),
            {"d": draft_id},
        )
        (call_id,) = new_call.scalars().all()
    discarded = await logged_in_client.post(f"/api/profile/drafts/{draft_id}/discard")
    assert discarded.status_code == 204
    async with engine.begin() as c:
        await c.execute(_AGE_DRAFT, {"d": draft_id, "days": 31})

    report = await retention_pass(engine, queue_app, migrated, cv_store)

    assert (report["draft_content"], report["cv_files"]) == (1, 1)
    assert (await draft_content(engine, draft_id))[:3] == (False, False, True)
    assert not await has_output(engine, call_id)
    assert not (cv_store_dir / path).exists()
