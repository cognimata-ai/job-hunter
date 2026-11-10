"""The deletion manifests' reconcilers (spec §17.4, §19.5; plan M5 Task 9, Deviation 43, D7):
`manifest_shipping` on every tick and `purge_fences` on the heavy cadence. Each runs in one transaction that
starts with the worker's epoch fence (`epoch.fence_epoch`, Ruling FR-h), and returns the rows it changed: the
manifests it shipped, the operations it created and the alerts it opened, raised or resolved.

**`manifest_shipping`** (every minute, Deviation 43; §17.4 says 5 min). A `completed` manifest that a
`manifest_shipment_items` row names with its uuid and an equal `file_sha256` becomes `shipped` with the
earliest such shipment (the backup container's shipper records one item per file its snapshot held, so the
coverage is exact, never inferred from a later or higher id: §18.2 "manifest shipment coverage"); `shipped_at`
is the time of the move. A `completed` manifest whose completion is older than `UNSHIPPED_AFTER` (1 h) at the
cadence's `now` with no such item opens `privacy.manifest_unshipped` (critical, subject `manifest:<uuid>`,
context `{manifest_id}`), once: a later pass that finds it still unshipped counts no occurrence. Every open
one whose manifest is no longer completed and overdue (it shipped) is resolved. It judges in every env: a dev
stack ships with an on-demand shipper (D19), and the UI reports a deletion as done only at `shipped`.
`observability.backup_health` reads the same state for the health: unshipped means `completed`.

**`purge_fences`** (D7). For each `executing` manifest, by its purge operations (`kind = 'purge'`, payload
`{manifest_id}`):

- its newest purge operation `failed`, or `running` with no heartbeat for `PURGE_STALE_AFTER` (1 h) at the
  cadence's `now`: `privacy.purge_failed` (critical, subject `manifest:<id>`, context `{manifest_id,
  operation_id, reason}`, reason `failed` or `stale`) is opened once; the fence is kept (fenced work stays
  blocked) and nothing is re-queued: the user retries it from Needs attention, which inserts a new operation;
- none of its purge operations `pending`, `running` or `failed` (none at all, or each cancelled, say by a
  superseding purge): a new one, `create_operation(kind="purge", payload={"manifest_id": id},
  dedup_key="purge:<uuid>")` with its job deferred, which resumes from the stored affected set, and the alert
  raised (reason `requeued`); but only while no unreleased `global` fence of another manifest stands (D5,
  controller ruling T9-2): delete-everything cancels the purges it supersedes, and their manifests are
  re-queued once its fence is released, when each finds nothing left and completes. The gate holds back the
  re-queue and its alert only; a failed or stale purge is alerted on as above.

The alert is resolved once its manifest leaves `executing`. Ids only: no alert, context or log line carries
anything but ids and counts.
"""

from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition_where
from jobhunter.observability.alerts import open_alert, open_subjects, raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.operations import PURGE_DEDUP_PREFIX, PURGE_KIND, create_operation
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import Deferrer

MANIFEST_UNSHIPPED_ALERT: Final = "privacy.manifest_unshipped"
PURGE_FAILED_ALERT: Final = "privacy.purge_failed"
UNSHIPPED_AFTER: Final = timedelta(hours=1)
PURGE_STALE_AFTER: Final = timedelta(hours=1)

# The earliest shipment of each completed manifest whose file a shipment item names with an equal hash;
# `manifest_shipment_items_uuid` serves the match.
_EARLIEST_SHIPMENT: Final = (
    "(SELECT DISTINCT ON (m.id) m.id AS manifest_id, s.id AS shipment_id FROM deletion_manifests m "
    "JOIN manifest_shipment_items i ON i.manifest_uuid = m.manifest_uuid AND i.file_sha256 = m.file_sha256 "
    "JOIN manifest_shipments s ON s.id = i.shipment_id WHERE m.status = 'completed' "
    "ORDER BY m.id, s.shipped_at, s.id) AS e"
)
_OVERDUE: Final = q(
    "SELECT id, manifest_uuid FROM deletion_manifests "
    "WHERE status = 'completed' AND completed_at < :cutoff ORDER BY id"
)
# Each executing manifest with its newest purge operation, whether any of them is pending, running or failed,
# and whether another manifest's global fence stands (the D5 gate); a manifest's operations are its payload's.
_EXECUTING: Final = q(
    "SELECT m.id, m.manifest_uuid, n.id AS operation_id, n.status, n.heartbeat_at, "
    "EXISTS (SELECT 1 FROM operations o WHERE o.kind = :kind AND o.payload -> 'manifest_id' = to_jsonb(m.id) "
    "AND o.status IN ('pending', 'running', 'failed')) AS blocks_requeue, "
    "EXISTS (SELECT 1 FROM purge_fences f WHERE f.kind = 'global' AND f.released_at IS NULL "
    "AND f.manifest_id <> m.id) AS under_global_fence "
    "FROM deletion_manifests m LEFT JOIN LATERAL (SELECT o.id, o.status, o.heartbeat_at FROM operations o "
    "WHERE o.kind = :kind AND o.payload -> 'manifest_id' = to_jsonb(m.id) "
    "ORDER BY o.id DESC LIMIT 1) n ON true "
    "WHERE m.status = 'executing' ORDER BY m.id"
)

log = get_logger("jobhunter.privacy.reconcilers")


def _manifest_subject(key: object) -> str:
    return f"manifest:{key}"


@reconciler("manifest_shipping", "tick")
async def manifest_shipping(ctx: ReconcileContext) -> int:
    """Ship the completed manifests a shipment item covers, and judge `privacy.manifest_unshipped` at
    `ctx.now` (module docstring); the manifests shipped and the alerts opened or resolved."""
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        shipped = await transition_where(
            conn,
            "deletion_manifests",
            ["completed"],
            "shipped",
            where="t.id = e.manifest_id",
            params={},
            from_sql=_EARLIEST_SHIPMENT,
            set_sql={"shipment_id": "e.shipment_id"},
            returning=("id",),
        )
        changed = len(shipped)
        overdue = (await conn.execute(_OVERDUE, {"cutoff": ctx.now - UNSHIPPED_AFTER})).all()
        for manifest in overdue:
            opened = await open_alert(
                conn,
                code=MANIFEST_UNSHIPPED_ALERT,
                severity="critical",
                subject=_manifest_subject(manifest.manifest_uuid),
                message=f"deletion manifest {manifest.id} was not shipped offsite within 1 h",
                context={"manifest_id": int(manifest.id)},
            )
            changed += opened is not None
        unshipped = {_manifest_subject(manifest.manifest_uuid) for manifest in overdue}
        for subject in await open_subjects(conn, code=MANIFEST_UNSHIPPED_ALERT):
            if subject not in unshipped:
                changed += await resolve_alert(conn, code=MANIFEST_UNSHIPPED_ALERT, subject=subject)
    if shipped:
        log.info("manifests_shipped", count=len(shipped))
    return changed


async def _requeue(conn: AsyncConnection, defer: Deferrer, manifest: Any) -> int:
    """A new purge operation for the manifest, its job deferred, and the alert raised; the rows changed."""
    operation_id, created = await create_operation(
        conn,
        kind=PURGE_KIND,
        payload={"manifest_id": int(manifest.id)},
        dedup_key=f"{PURGE_DEDUP_PREFIX}{manifest.manifest_uuid}",
        defer=defer,
    )
    await raise_alert(
        conn,
        code=PURGE_FAILED_ALERT,
        severity="critical",
        subject=_manifest_subject(manifest.id),
        message=f"deletion manifest {manifest.id} had no live purge operation; one was queued",
        context={"manifest_id": int(manifest.id), "operation_id": operation_id, "reason": "requeued"},
    )
    log.warning("purge_requeued", manifest_id=int(manifest.id), operation_id=operation_id)
    return int(created) + 1


_STUCK: Final = {"failed": "failed", "stale": "has not progressed for 1 h"}


def _stuck_reason(manifest: Any, now: datetime) -> str | None:
    """`failed` or `stale` for a manifest whose newest purge operation is stuck so, else `None`."""
    if manifest.status == "failed":
        return "failed"
    if manifest.status == "running" and manifest.heartbeat_at < now - PURGE_STALE_AFTER:
        return "stale"
    return None


@reconciler("purge_fences", "heavy")
async def purge_fences(ctx: ReconcileContext) -> int:
    """Alert on the stuck purges of the executing manifests, re-queue the ones without an operation (unless
    another manifest's global fence stands), and resolve the alerts of the manifests that left `executing`, at
    `ctx.now` (module docstring); the operations created and the alerts opened, raised or resolved."""
    changed = 0
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        executing = (await conn.execute(_EXECUTING, {"kind": PURGE_KIND})).all()
        for manifest in executing:
            if not manifest.blocks_requeue:
                if (
                    not manifest.under_global_fence
                ):  # D5: re-queued once delete-everything's fence is released
                    changed += await _requeue(conn, ctx.defer, manifest)
                continue
            reason = _stuck_reason(manifest, ctx.now)
            if reason is None:
                continue
            opened = await open_alert(
                conn,
                code=PURGE_FAILED_ALERT,
                severity="critical",
                subject=_manifest_subject(manifest.id),
                message=f"the purge of deletion manifest {manifest.id} {_STUCK[reason]}; its fence is kept",
                context={
                    "manifest_id": int(manifest.id),
                    "operation_id": int(manifest.operation_id),
                    "reason": reason,
                },
            )
            changed += opened is not None
        live = {_manifest_subject(manifest.id) for manifest in executing}
        for subject in await open_subjects(conn, code=PURGE_FAILED_ALERT):
            if subject not in live:
                changed += await resolve_alert(conn, code=PURGE_FAILED_ALERT, subject=subject)
    return changed
