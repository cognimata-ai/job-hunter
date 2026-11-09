"""The alerts nothing raised before M5 and the auto-resolves that were missing (spec §17.6; plan M5 Task 3,
O-ops O12, O17, Q11, Q18, M1 Carry-forward 11, M2 Carry-forward 3).

The worker's heavy `metrics_refresh` raises, each with its subject:

- `system.disk_pressure` (`disk`): warn at 80 % of the data volumes' filesystem, escalated to critical at
  90 %, resolved below 80 %; the filesystem is measured where the CV store is (`shutil.disk_usage`, patched
  here, read as `df` reads it: used of used plus available);
- `system.table_growth` (info, `table:<name>`): `procrastinate_jobs` above its row estimate limit, or any
  table above its size limit, each table its own alert; resolved below;
- `embedder.checksum_mismatch` (critical, `embedder`): the embedder's `/health` (patched here) with
  `checksum_ok: false`, resolved by `true`; an unreachable embedder neither raises nor resolves it;
- `docparse.leftover` (warn, `docparse`): while docparse's marker is in the parse root.

And three alerts resolve themselves now: `scoring.canary_drift` per case once its next canary lies within
bounds, `batch.submit_ambiguous_unresolved` in the adoption transaction, `dedup.ambiguous_grouping` once the
job closes. Every datum is invented.
"""

import copy
import errno
import os
import shutil
import socket
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.availability.rules import recompute_job_availability
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db import ranking
from jobhunter.db.transitions import transition
from jobhunter.dedup.merge import merge_jobs, unmerge
from jobhunter.embeddings import (
    EMBEDDING_MODEL_ID,
    NO_HEALTH,
    EmbedderClient,
    EmbedderHealth,
    make_http_client,
)
from jobhunter.errors import FencedOut
from jobhunter.llm.fake import BatchScript
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.metrics import ALERTS_OPEN, REGISTRY, refresh_table_bytes
from jobhunter.scoring import eval_runner, golden
from jobhunter.worker import reconcilers as r
from jobhunter.worker import tasks
from jobhunter.worker.runtime import ShutdownState
from jobhunter.worker.tasks import defer_in_tx
from procrastinate.job_context import JobContext
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.batches import (
    PUBLISHED_BODY,
    bulk_with_targets,
    reconcile_ambiguous_once,
    submission_rows,
)
from tests.support.collect import pending_item, queued_run, resolved
from tests.support.eval import (
    CaseRef,
    eval_deps,
    eval_generator_body,
    execute,
    fake_eval_answers,
    insert_cases,
)
from tests.support.faults import no_fault
from tests.support.llm import FakeLLM, score_profile
from tests.support.logs import logged
from tests.support.pg import PgServer, pg_one
from tests.support.profile import OpRunner
from tests.support.scoring import activate_score_tuple, activate_test_policy
from tests.support.validation import synthetic_ad

pytestmark = pytest.mark.integration

DISK: Final = "system.disk_pressure"
GROWTH: Final = "system.table_growth"
CHECKSUM: Final = "embedder.checksum_mismatch"
LEFTOVER: Final = "docparse.leftover"
DRIFT: Final = "scoring.canary_drift"
UNRESOLVED: Final = "batch.submit_ambiguous_unresolved"
GROUPING: Final = "dedup.ambiguous_grouping"
_ALERTS: Final = sa.text(
    "SELECT id, severity, dedup_key, occurrences, context, resolved_at FROM alerts WHERE code = :c "
    "ORDER BY id"
)


async def alerts(engine: AsyncEngine, code: str) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_ALERTS, {"c": code})).all())


def open_keys(rows: list[Any]) -> set[str]:
    return {a.dedup_key for a in rows if a.resolved_at is None}


async def refresh(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    *,
    cfg: AppConfig | None = None,
    embedder: EmbedderClient | None = None,
) -> None:
    """One `metrics_refresh` of epoch 1 (the current one), with the worker's config and embedder client."""
    ctx = r.ReconcileContext(
        engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1, embedder=embedder, cfg=cfg
    )
    assert await r.metrics_refresh(ctx) == 0  # it repairs nothing


@pytest.fixture
def cfg(make_config: Any, tmp_path: Path) -> AppConfig:
    """The worker's config, its CV store and parse root directories of the test."""
    (tmp_path / "cv").mkdir()
    (tmp_path / "parse").mkdir()
    config: AppConfig = make_config(
        "worker",
        JOBHUNTER_CV_STORE_DIR=str(tmp_path / "cv"),
        JOBHUNTER_PARSE_JOBS_DIR=str(tmp_path / "parse"),
    )
    return config


class Disk:
    """`shutil.disk_usage`, patched: `used` and available blocks, per mille of what is not reserved, of a
    filesystem whose total also counts its reserved blocks (as `df` sees one); the paths it was asked for."""

    def __init__(self) -> None:
        self.used = 0
        self.unreadable = False
        self.paths: list[Path] = []

    def __call__(self, path: Path | str) -> SimpleNamespace:
        self.paths.append(Path(path))
        if self.unreadable:
            raise FileNotFoundError(2, "No such file or directory")
        return SimpleNamespace(total=2000, used=self.used, free=1000 - self.used)


@pytest.fixture
def disk(monkeypatch: pytest.MonkeyPatch) -> Disk:
    fake = Disk()
    monkeypatch.setattr(shutil, "disk_usage", fake)  # as `metrics_refresh` calls it
    return fake


@pytest.fixture
async def embedder() -> AsyncIterator[EmbedderClient]:
    """An embedder client on a port nothing listens on: unreachable unless its `health` is patched."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    async with make_http_client() as http:
        yield EmbedderClient(f"http://127.0.0.1:{port}", http)


def answering(checksum_ok: bool) -> Callable[..., Coroutine[Any, Any, EmbedderHealth]]:
    """An embedder `/health` that answers with its model and `checksum_ok`."""

    async def health(timeout_s: float = 0.0) -> EmbedderHealth:
        return EmbedderHealth(ok=checksum_ok, model_id=EMBEDDING_MODEL_ID, checksum_ok=checksum_ok)

    return health


def open_by_severity(severity: str) -> float | None:
    return REGISTRY.get_sample_value("jobhunter_alerts_open", {"severity": severity})


# ---- system.disk_pressure ----------------------------------------------------------------------------------


async def test_disk_pressure_warns_at_80_and_escalates_at_90(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, cfg: AppConfig, disk: Disk
) -> None:
    assert (r.DISK_WARN_RATIO, r.DISK_CRITICAL_RATIO) == (0.80, 0.90)
    disk.used = 799
    await refresh(engine, queue_app, cfg=cfg)
    assert await alerts(engine, DISK) == []
    disk.used = 800  # 80 % used, though 40 % of the total with the reserved blocks
    await refresh(engine, queue_app, cfg=cfg)
    [warned] = await alerts(engine, DISK)
    assert (warned.severity, warned.dedup_key, warned.occurrences, warned.resolved_at) == (
        "warn",
        f"{DISK}:disk",
        1,
        None,
    )
    assert warned.context == {"used_pct": 80.0}
    disk.used = 899
    await refresh(engine, queue_app, cfg=cfg)
    [again] = await alerts(engine, DISK)
    assert (again.id, again.severity, again.occurrences, again.context) == (
        warned.id,
        "warn",
        2,
        warned.context,
    )
    disk.used = 900
    await refresh(engine, queue_app, cfg=cfg)
    [escalated] = await alerts(engine, DISK)  # the open alert escalated in place
    assert (escalated.id, escalated.severity, escalated.occurrences, escalated.resolved_at) == (
        warned.id,
        "critical",
        3,
        None,
    )
    assert escalated.context == {"used_pct": 90.0}
    assert disk.paths == [cfg.cv_store_dir] * 4  # the volumes' filesystem, where the CV store is


async def test_disk_pressure_resolves_below_80(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, cfg: AppConfig, disk: Disk
) -> None:
    disk.used = 950
    await refresh(engine, queue_app, cfg=cfg)
    disk.used = 850  # below 90 %: it never de-escalates
    await refresh(engine, queue_app, cfg=cfg)
    disk.used = 800
    await refresh(engine, queue_app, cfg=cfg)
    [alert] = await alerts(engine, DISK)
    assert (alert.severity, alert.occurrences, alert.resolved_at) == ("critical", 3, None)
    disk.used = 799
    await refresh(engine, queue_app, cfg=cfg)
    [alert] = await alerts(engine, DISK)
    assert alert.resolved_at is not None
    disk.used = 800  # a later pressure is a new alert, at warn
    await refresh(engine, queue_app, cfg=cfg)
    assert [(a.severity, a.resolved_at is None) for a in await alerts(engine, DISK)] == [
        ("critical", False),
        ("warn", True),
    ]


async def test_an_unreadable_disk_neither_raises_nor_resolves(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    cfg: AppConfig,
    disk: Disk,
    caplog: pytest.LogCaptureFixture,
) -> None:
    disk.used = 900
    await refresh(engine, queue_app, cfg=cfg)
    disk.unreadable = True
    await refresh(engine, queue_app, cfg=cfg)
    [alert] = await alerts(engine, DISK)
    assert (alert.severity, alert.occurrences, alert.resolved_at) == ("critical", 1, None)
    [event] = logged(caplog, "disk_usage_unreadable")
    assert event["error_class"] == "FileNotFoundError"


# ---- system.table_growth -----------------------------------------------------------------------------------


QUEUED: Final = 40
_QUEUE_JOBS: Final = (
    "INSERT INTO procrastinate_jobs (queue_name, task_name) "
    "SELECT 'aux', 'test:noop' FROM generate_series(1, %s)"
)


async def test_table_growth_is_info_per_table(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    migrated: PgServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (r.QUEUE_ROWS_LIMIT, r.TABLE_BYTES_LIMIT) == (1_000_000, 10_000_000_000)  # §17.6's 1M rows, 10 GB
    default_bytes = r.TABLE_BYTES_LIMIT

    # the queue's row estimate is the planner's (`pg_class.reltuples`), which ANALYZE sets: 40 queued jobs
    pg_one(migrated, "superuser", _QUEUE_JOBS, QUEUED)
    pg_one(migrated, "superuser", "ANALYZE procrastinate_jobs")
    monkeypatch.setattr(r, "QUEUE_ROWS_LIMIT", QUEUED - 1)
    await refresh(engine, queue_app)
    [queue] = await alerts(engine, GROWTH)
    assert (queue.severity, queue.dedup_key, queue.resolved_at) == (
        "info",
        f"{GROWTH}:table:procrastinate_jobs",
        None,
    )
    assert queue.context == {"table": "procrastinate_jobs", "rows": QUEUED}  # read from the queue table
    monkeypatch.setattr(r, "QUEUE_ROWS_LIMIT", QUEUED)  # at the limit, not above it: resolved
    await refresh(engine, queue_app)
    assert open_keys(await alerts(engine, GROWTH)) == set()
    await refresh(engine, queue_app)  # and not raised again
    assert len(await alerts(engine, GROWTH)) == 1

    async with engine.connect() as conn:
        sizes = await refresh_table_bytes(conn)
    limit = sorted(set(sizes.values()))[-2]  # only the largest tables are above it
    over = {table for table, size in sizes.items() if size > limit}
    assert over and len(over) < len(sizes)
    monkeypatch.setattr(r, "TABLE_BYTES_LIMIT", limit)
    await refresh(engine, queue_app)
    opened = [a for a in await alerts(engine, GROWTH) if a.resolved_at is None]
    assert {a.dedup_key for a in opened} == {f"{GROWTH}:table:{table}" for table in over}  # one per table
    for alert in opened:
        assert alert.severity == "info"
        assert (
            f"{GROWTH}:table:{alert.context['table']}" == alert.dedup_key and alert.context["bytes"] > limit
        )
    monkeypatch.setattr(r, "TABLE_BYTES_LIMIT", default_bytes)
    await refresh(engine, queue_app)
    assert open_keys(await alerts(engine, GROWTH)) == set()


# ---- embedder.checksum_mismatch ----------------------------------------------------------------------------


async def test_checksum_mismatch_raises_and_resolves(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    embedder: EmbedderClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedder, "health", answering(False))
    await refresh(engine, queue_app, embedder=embedder)
    [alert] = await alerts(engine, CHECKSUM)
    assert (alert.severity, alert.dedup_key, alert.resolved_at) == ("critical", f"{CHECKSUM}:embedder", None)
    assert open_by_severity("critical") == 1  # the gauge, refreshed after the pass's own alerts
    monkeypatch.setattr(embedder, "health", answering(True))
    await refresh(engine, queue_app, embedder=embedder)
    [alert] = await alerts(engine, CHECKSUM)
    assert alert.resolved_at is not None
    assert open_by_severity("critical") == 0


async def test_an_unreachable_embedder_neither_raises_nor_resolves(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, embedder: EmbedderClient
) -> None:
    assert await embedder.health() == NO_HEALTH  # no answer: its checksum is unknown
    await refresh(engine, queue_app, embedder=embedder)
    assert await alerts(engine, CHECKSUM) == []
    async with engine.begin() as conn:
        await raise_alert(conn, code=CHECKSUM, severity="critical", subject="embedder", message="mismatch")
    await refresh(engine, queue_app, embedder=embedder)
    [alert] = await alerts(engine, CHECKSUM)
    assert (alert.occurrences, alert.resolved_at) == (1, None)


# ---- docparse.leftover -------------------------------------------------------------------------------------


async def test_the_docparse_marker_raises_and_its_removal_resolves(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App, cfg: AppConfig, disk: Disk
) -> None:
    assert r.DOCPARSE_LEFTOVER_MARKER == ".docparse-leftover"
    (cfg.parse_jobs_dir / "job-1").mkdir()  # a job is no marker
    await refresh(engine, queue_app, cfg=cfg)
    assert await alerts(engine, LEFTOVER) == []
    marker = cfg.parse_jobs_dir / r.DOCPARSE_LEFTOVER_MARKER
    marker.touch(mode=0o660)
    await refresh(engine, queue_app, cfg=cfg)
    [alert] = await alerts(engine, LEFTOVER)
    assert (alert.severity, alert.dedup_key, alert.resolved_at) == ("warn", f"{LEFTOVER}:docparse", None)
    marker.unlink()  # docparse's next start swept clean
    await refresh(engine, queue_app, cfg=cfg)
    [alert] = await alerts(engine, LEFTOVER)
    assert alert.resolved_at is not None


# ---- the epoch fence ---------------------------------------------------------------------------------------


async def test_a_superseded_worker_writes_no_system_alert(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    cfg: AppConfig,
    disk: Disk,
    embedder: EmbedderClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its writes are fenced by the worker epoch like every reconciler's: a superseded worker raises
    `FencedOut` (its cadence stops there) having written nothing."""
    disk.used = 950
    (cfg.parse_jobs_dir / r.DOCPARSE_LEFTOVER_MARKER).touch()
    monkeypatch.setattr(embedder, "health", answering(False))
    superseded = r.ReconcileContext(
        engine=engine,
        queue=queue_app,
        defer=defer_in_tx,
        now=utcnow(),
        epoch=current_epoch + 1,
        embedder=embedder,
        cfg=cfg,
    )
    async with engine.begin() as conn:  # one open alert the gauge must count
        await raise_alert(conn, code=CHECKSUM, severity="critical", subject="embedder", message="mismatch")
    ALERTS_OPEN.labels(severity="critical").set(99)  # stale, until a refresh
    with pytest.raises(FencedOut) as fenced:
        await r.metrics_refresh(superseded)
    assert fenced.value.worker_superseded
    for code in (DISK, LEFTOVER):
        assert await alerts(engine, code) == []
    [alert] = await alerts(engine, CHECKSUM)
    assert alert.occurrences == 1  # not counted again
    assert open_by_severity("critical") == 1  # the gauge, refreshed before the fenced writes


async def test_an_unreadable_marker_leaves_the_other_checks_and_the_gauge_whole(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    cfg: AppConfig,
    disk: Disk,
    embedder: EmbedderClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ruling T3-4 (I1): a marker probe that fails otherwise than "not found" (here `EACCES` on the parse
    root) is logged and judges neither way; the disk, the checksum, the table growth and the gauge go on."""
    async with engine.begin() as conn:  # docparse failed earlier
        await raise_alert(conn, code=LEFTOVER, severity="warn", subject="docparse", message="leftover")
    [before] = await alerts(engine, LEFTOVER)
    real_lstat = os.lstat

    def refusing(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if Path(path).name == r.DOCPARSE_LEFTOVER_MARKER:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", refusing)
    disk.used = 900
    monkeypatch.setattr(embedder, "health", answering(False))
    monkeypatch.setattr(r, "TABLE_BYTES_LIMIT", 0)  # every table is above it
    await refresh(engine, queue_app, cfg=cfg, embedder=embedder)
    assert open_keys(await alerts(engine, DISK)) == {f"{DISK}:disk"}
    assert open_keys(await alerts(engine, CHECKSUM)) == {f"{CHECKSUM}:embedder"}
    growth = open_keys(await alerts(engine, GROWTH))
    assert f"{GROWTH}:table:alerts" in growth
    assert await alerts(engine, LEFTOVER) == [before]  # exactly as it was: neither counted nor resolved
    [event] = logged(caplog, "docparse_marker_unreadable")
    assert event["error_class"] == "PermissionError"
    # the gauge counts this pass's alerts: the disk and the checksum, the leftover, one per table
    assert (open_by_severity("critical"), open_by_severity("warn"), open_by_severity("info")) == (
        2,
        1,
        len(growth),
    )


async def test_a_check_whose_writes_fail_leaves_the_others_and_fails_after_the_commit(
    engine: AsyncEngine,
    current_epoch: int,
    queue_app: procrastinate.App,
    cfg: AppConfig,
    disk: Disk,
    embedder: EmbedderClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ruling T3-4 (I1): each check writes in a savepoint of its own. A database error in one (here the
    disk's) rolls back that check only; the others commit, the gauge counts them, and the reconciler raises
    the error after the commit, so `run_reconcilers` records `reconciler.failed`."""

    async def failing(conn: AsyncConnection, used: float) -> None:
        await conn.execute(sa.text("SELECT 1 / 0"))

    monkeypatch.setattr(r, "_disk_pressure", failing)
    disk.used = 950
    monkeypatch.setattr(embedder, "health", answering(False))
    (cfg.parse_jobs_dir / r.DOCPARSE_LEFTOVER_MARKER).touch()
    ctx = r.ReconcileContext(
        engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1, embedder=embedder, cfg=cfg
    )
    with pytest.raises(sa.exc.DBAPIError):
        await r.metrics_refresh(ctx)
    assert await alerts(engine, DISK) == []
    assert open_keys(await alerts(engine, CHECKSUM)) == {f"{CHECKSUM}:embedder"}  # committed
    assert open_keys(await alerts(engine, LEFTOVER)) == {f"{LEFTOVER}:docparse"}
    assert (open_by_severity("critical"), open_by_severity("warn")) == (1, 1)
    [event] = logged(caplog, "system_alert_write_failed")
    assert (event["check"], event["error_class"]) == ("disk", "DataError")


# ---- scoring.canary_drift ----------------------------------------------------------------------------------

BY_8_75: Final = ("skills", "domain")  # +1 level: 25 / 4 + 10 / 4 points, beyond the 8 of a drift
BY_7_5: Final = ("role_fit", "domain")  # +1 level: 20 / 4 + 10 / 4 points, within them


def canary_answer(fake_llm: FakeLLM, case: CaseRef, raised: tuple[str, ...] = ()) -> None:
    """The case's generator answer: every criterion at level 2, those of `raised` at 3."""
    body: dict[str, Any] = copy.deepcopy(
        eval_generator_body("canary", synthetic_ad().description_text, score_profile(), level=2)
    )
    for criterion in body["criteria"]:
        if criterion["criterion"] in raised:
            criterion["level"] = 3
    fake_llm.answer("generate", case.job_revision_id, body)
    for round_ in (1, 2):
        fake_llm.answer("revise", case.job_revision_id, body, round=round_)


_EVAL_RUNS: Final = sa.text("SELECT id FROM eval_runs ORDER BY id")
_EVAL_OPERATION: Final = sa.text("SELECT id FROM operations WHERE payload ->> 'eval_run_id' = :r")


async def canary_run(engine: AsyncEngine, fake_llm: FakeLLM, epoch: int) -> None:
    """One `canary_daily`, then its operation executed."""
    async with engine.connect() as conn:
        before = set((await conn.execute(_EVAL_RUNS)).scalars())
    runtime = SimpleNamespace(engine=engine, cfg=fake_llm.cfg, epoch=epoch, shutdown=ShutdownState())
    await tasks.canary_daily(
        cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime})), timestamp=0
    )
    async with engine.connect() as conn:
        [run_id] = set((await conn.execute(_EVAL_RUNS)).scalars()) - before
        operation_id = (await conn.execute(_EVAL_OPERATION, {"r": str(run_id)})).scalar_one()
    await execute(engine, operation_id, eval_deps(engine, fake_llm), epoch=epoch)


async def test_canary_drift_resolves_on_an_in_bounds_canary(
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    current_epoch: int,
    queue_app: procrastinate.App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(eval_runner, "afault_point", no_fault)
    async with engine.begin() as conn:
        await activate_test_policy(conn)
    made = await insert_cases(engine, 3)
    back, still = made[0], made[1]
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
        for case in (back, still):
            await golden.set_flags(conn, case.id, active=None, is_canary=True, now=utcnow())
    await fake_eval_answers(engine, fake_llm)  # the critic's answers
    for case in (back, still):
        canary_answer(fake_llm, case)
    await canary_run(engine, fake_llm, current_epoch)  # the baselines
    for case in (back, still):
        canary_answer(fake_llm, case, BY_8_75)
    await canary_run(engine, fake_llm, current_epoch)
    assert open_keys(await alerts(engine, DRIFT)) == {f"{DRIFT}:{back.id}", f"{DRIFT}:{still.id}"}

    canary_answer(fake_llm, back, BY_7_5)  # back within bounds of its baseline; the other still drifts
    await canary_run(engine, fake_llm, current_epoch)
    by_key = {a.dedup_key: a for a in await alerts(engine, DRIFT)}
    assert len(by_key) == 2
    assert by_key[f"{DRIFT}:{back.id}"].resolved_at is not None
    assert (by_key[f"{DRIFT}:{still.id}"].occurrences, by_key[f"{DRIFT}:{still.id}"].resolved_at) == (2, None)


# ---- batch.submit_ambiguous_unresolved ---------------------------------------------------------------------

BATCH_TARGETS: Final = 250  # as the ambiguous-submit tests: enough for a batch


async def test_adoption_resolves_submit_ambiguous_unresolved(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queue_app: procrastinate.App
) -> None:
    made = await bulk_with_targets(engine, BATCH_TARGETS)
    for target in made.targets:
        fake_llm.answer("generate", target.job_revision_id, PUBLISHED_BODY)
    fake_llm.batches(BatchScript(create="ambiguous_created", polls_until_ended=0))  # created, its answer lost
    assert made.operation_id is not None
    await op_runner.run_one(made.operation_id)
    [row] = await submission_rows(engine)
    assert row.state == "submit_ambiguous"
    started = row.submit_started_at
    fake_llm.batches(BatchScript(listing="error_500", polls_until_ended=0))  # the listing fails for 25 h
    report = await reconcile_ambiguous_once(engine, fake_llm, now=started + timedelta(hours=25, seconds=1))
    assert (report.unresolved, report.adopted) == (1, 0)
    [alert] = await alerts(engine, UNRESOLVED)
    assert (alert.severity, alert.dedup_key, alert.resolved_at) == (
        "critical",
        f"{UNRESOLVED}:{row.id}",
        None,
    )

    fake_llm.batches(BatchScript(polls_until_ended=0))  # the listing answers again: the batch is found
    report = await reconcile_ambiguous_once(engine, fake_llm, now=started + timedelta(hours=26))
    assert report.adopted == 1
    [alert] = await alerts(engine, UNRESOLVED)
    assert alert.resolved_at is not None
    [row] = await submission_rows(engine)
    assert row.state == "reconciled"


# ---- dedup.ambiguous_grouping ------------------------------------------------------------------------------


_JOB_OF_POSTING: Final = sa.text("SELECT job_id FROM postings WHERE id = :p")
_POSTING_OF_ITEM: Final = sa.text(
    "SELECT o.posting_id, p.job_id FROM posting_observations o JOIN postings p ON p.id = o.posting_id "
    "WHERE o.raw_item_id = :i"
)
_JOB_AVAILABILITY: Final = sa.text("SELECT availability FROM jobs WHERE id = :j")


async def job_availability(engine: AsyncEngine, job_id: int) -> str:
    async with engine.connect() as conn:
        return str((await conn.execute(_JOB_AVAILABILITY, {"j": job_id})).scalar_one())


async def test_closing_a_job_resolves_ambiguous_grouping(
    engine: AsyncEngine, current_epoch: int, posting_factory: Any
) -> None:
    seen = utcnow() - timedelta(days=1)
    postings = {
        name: await posting_factory(
            engine,
            source_id="greenhouse",
            native_id=name,
            availability="active",
            last_seen_at=seen,
            availability_changed_at=seen,
        )
        for name in ("closing", "fading")
    }
    async with engine.begin() as conn:
        jobs = {
            name: int((await conn.execute(_JOB_OF_POSTING, {"p": p})).scalar_one())
            for name, p in postings.items()
        }
        for job in jobs.values():  # each joined the best of several groups
            await raise_alert(conn, code=GROUPING, severity="info", subject=f"job:{job}", message="ambiguous")
    async with engine.begin() as conn:
        await transition(conn, "postings", postings["closing"], ["active"], "closed", reason="ats_absent")
        await transition(conn, "postings", postings["fading"], ["active"], "stale")
        for job in jobs.values():
            assert await recompute_job_availability(conn, job) is True
    by_key = {a.dedup_key: a for a in await alerts(engine, GROUPING)}
    assert by_key[f"{GROUPING}:job:{jobs['closing']}"].resolved_at is not None
    assert by_key[f"{GROUPING}:job:{jobs['fading']}"].resolved_at is None  # a stale job's choice still stands


async def test_an_unmerge_that_closes_a_job_resolves_ambiguous_grouping(
    engine: AsyncEngine, current_epoch: int
) -> None:
    """The dedup path (ruling T3-3): an unmerge takes the merged posting back, and the job whose own posting
    is closed closes with it, its availability derived where every path derives it
    (`merge.derive_from_postings`)."""
    owner = await queued_run(engine)
    at = utcnow() - timedelta(hours=8)
    found: list[tuple[int, int]] = []
    for hours, native_id in enumerate(("UA", "UB")):  # two JSearch postings, each its own job
        item = await pending_item(
            engine, owner, "jsearch", {"native_id": native_id}, fetched_at=at + timedelta(hours=hours)
        )
        assert await resolved(engine, owner, item) == "linked"
        async with engine.connect() as conn:
            found.append(tuple((await conn.execute(_POSTING_OF_ITEM, {"i": item})).one()))
    (closing_posting, into), (_, from_) = found
    async with engine.begin() as conn:  # the job's own posting closes
        await transition(conn, "postings", closing_posting, ["active"], "closed", reason="ats_absent")
        assert await recompute_job_availability(conn, into) is True
    async with engine.begin() as conn:
        event = await merge_jobs(
            conn, from_job_id=from_, into_job_id=into, kind="user_confirmed_merge", evidence={}
        )
        await ranking.bump(conn)
    assert await job_availability(engine, into) == "active"  # the merged posting is active
    async with engine.begin() as conn:  # the job then joined the best of several groups
        await raise_alert(conn, code=GROUPING, severity="info", subject=f"job:{into}", message="ambiguous")
    async with engine.begin() as conn:
        await unmerge(conn, event)  # its own, closed posting alone again
    assert await job_availability(engine, into) == "closed"
    [alert] = await alerts(engine, GROUPING)
    assert (alert.dedup_key, alert.resolved_at is not None) == (f"{GROUPING}:job:{into}", True)
