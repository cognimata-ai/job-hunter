"""Source instances (spec §7.5): their rows, the provider-secret sync and the instances of watchlist boards.

`sync_instances` applies plan Deviation 6 (a behaviour change approved by the user): a paid or quota
`default` instance whose provider secret is absent is `disable`d with `last_error_code = 'missing_secret'`
(an already disabled one keeps its own code), and an instance is restored to `ok` only from exactly that
state once its secret is present again. A paid instance's disable (JSearch, SerpApi and Adzuna: every
budgeted source, Global Constraints) also opens `source.auth_failed` (critical, code `missing_secret`;
Ruling FR-t), and a start that finds a paid instance already disabled for its still missing secret opens
that alert when none is open (a database that an older worker started without the key, or an alert resolved
by hand), never one more occurrence. The restore leaves it open, and the restored key's first success
resolves it (§17.6). Every other state is left untouched by the restore: an administrative `disabled` with
another code, and every breaker state with its timestamps. In fake mode (`JOBHUNTER_FAKE_SOURCES`) the
fixtures need no secret, so every secret counts as present (Deviation 5).

The sync is worker-owned work: its `guard` (the worker's epoch fence, `epoch.fence_epoch`) runs first, so a
superseded worker, whose view of the secrets is stale, can never restore an instance that its successor
disabled for a missing secret (final review C2). It locks no job row, so the guard may come first (Ruling
T19-a).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.config import AppConfig
from jobhunter.db.guards import Guard
from jobhunter.db.seed import DEFAULT_INSTANCES, SOURCES
from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.sources.base import MISSING_SECRET
from jobhunter.sources.breaker import ALERTS, Health, raise_missing_secret, resolve_alerts

# The §19.2 secret files a source's `default` instance cannot send without; the free sources need none.
PROVIDER_SECRETS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {"jsearch": ("rapidapi_key",), "serpapi": ("serpapi_key",), "adzuna": ("adzuna_app_id", "adzuna_app_key")}
)

# The alerts a disable leaves open, by its code: the credential problem a missing secret still is.
_KEPT_ON_DISABLE: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {MISSING_SECRET: frozenset({ALERTS["auth_failed"][0]})}
)

# The sources whose missing secret also opens `source.auth_failed` (Ruling FR-t): the paid ones, every
# budgeted source (Global Constraints): `paid_search` (JSearch, SerpApi) and `quota_search` (Adzuna).
_PAID: Final = frozenset(source.id for source in SOURCES if source.kind in ("paid_search", "quota_search"))

_log = get_logger("jobhunter.sources.instances")


@dataclass(frozen=True)
class InstanceRow:
    id: int
    source_id: str
    instance_key: str
    watchlist_company_id: int | None
    health: Health
    half_open_after: datetime | None
    board_slug: str | None  # the watchlist company's board, for an ATS instance
    lever_region: str | None
    display_name: str | None


_LIST = q(
    "SELECT i.id, i.source_id, i.instance_key, i.watchlist_company_id, i.health, i.half_open_after, "
    "w.board_slug, w.lever_region, w.display_name "
    "FROM source_instances i LEFT JOIN watchlist_companies w ON w.id = i.watchlist_company_id ORDER BY i.id"
)
_DISABLE = q(
    "UPDATE source_instances SET health = 'disabled', last_error_code = :code, breaker_opened_at = NULL, "
    "half_open_after = NULL, consecutive_failures = 0 WHERE id = :id RETURNING id"
)
_DEFAULTS = q(
    "SELECT id, source_id, health, last_error_code FROM source_instances "
    "WHERE instance_key = 'default' AND source_id = ANY(:sources) ORDER BY id FOR UPDATE"
)
_RESTORE = q(
    "UPDATE source_instances SET health = 'ok', last_error_code = NULL, consecutive_failures = 0 "
    "WHERE id = :id AND health = 'disabled' AND last_error_code = :code"
)
_COMPANY = q("SELECT ats_type, board_slug, lever_region FROM watchlist_companies WHERE id = :id")
_FIND = q(
    "SELECT id, watchlist_company_id FROM source_instances "
    "WHERE source_id = :source_id AND instance_key = :key"
)
_LINK = q(
    "UPDATE source_instances SET watchlist_company_id = :company_id "
    "WHERE id = :id AND watchlist_company_id IS NULL"
)
_INSERT = q(
    "INSERT INTO source_instances (source_id, instance_key, watchlist_company_id) "
    "VALUES (:source_id, :key, :company_id) ON CONFLICT (source_id, instance_key) DO NOTHING RETURNING id"
)


async def list_instances(conn: AsyncConnection) -> list[InstanceRow]:
    """Every source instance, with its watchlist company's board and name, in id order."""
    return [InstanceRow(**row) for row in (await conn.execute(_LIST)).mappings()]


async def disable(conn: AsyncConnection, instance_id: int, *, code: str) -> None:
    """`disabled` with `last_error_code = code`, the breaker cleared (no timestamps, no failures) and the
    alert of the open state it left resolved: a disabled instance is no open breaker
    (`breaker.resolve_alerts`). A disable for a missing secret keeps an open `source.auth_failed`: the
    credential is still missing, §17.6 resolves that alert on a success only, and a missing credential stays
    visible (Ruling FR-l); the restored key's first success resolves it."""
    if (await conn.execute(_DISABLE, {"id": instance_id, "code": code})).first() is None:
        raise LookupError(f"source instance {instance_id} does not exist")
    await resolve_alerts(conn, instance_id, keep=_KEPT_ON_DISABLE.get(code, frozenset()))


def _secrets_present(cfg: AppConfig, source_id: str) -> bool:
    return cfg.fake_sources or all(cfg.secrets.get(name) for name in PROVIDER_SECRETS.get(source_id, ()))


async def sync_instances(conn: AsyncConnection, cfg: AppConfig, *, guard: Guard) -> dict[str, Health]:
    """Disable or restore the `default` instances by their provider secrets (plan Deviation 6).

    Returns the health of every `default` instance afterwards, by source id. `guard` runs first, before any
    instance is read or locked; a `FencedOut` from it propagates, nothing written. The caller owns the
    transaction.
    """
    await guard(conn)
    health: dict[str, Health] = {}
    rows = (await conn.execute(_DEFAULTS, {"sources": list(DEFAULT_INSTANCES)})).all()
    for row in rows:
        health[row.source_id] = row.health
        present = _secrets_present(cfg, row.source_id)
        if not present and row.health != "disabled":
            await disable(conn, row.id, code=MISSING_SECRET)
            if row.source_id in _PAID:  # Ruling FR-t: visible as an open alert, under the same fence
                await raise_missing_secret(conn, row.id, row.source_id)
            health[row.source_id] = "disabled"
            _log.warning("source_instance_disabled", source_id=row.source_id, code=MISSING_SECRET)
        elif not present and row.last_error_code == MISSING_SECRET and row.source_id in _PAID:
            # Already disabled for this missing key: its alert stays open, opened again only when none is
            # (Ruling FR-t; F5 re-review), under the same fence and with no occurrence counted.
            if await raise_missing_secret(conn, row.id, row.source_id, if_not_open=True):
                _log.warning("missing_secret_alert_opened", source_id=row.source_id, code=MISSING_SECRET)
        elif present and row.health == "disabled" and row.last_error_code == MISSING_SECRET:
            await conn.execute(_RESTORE, {"id": row.id, "code": MISSING_SECRET})
            health[row.source_id] = "ok"
            _log.info("source_instance_restored", source_id=row.source_id)
    return health


def _board_instance_key(ats_type: str, board_slug: str, lever_region: str | None) -> str:
    """The `instance_key` of a watchlist board: the slug, or `<region>:<slug>` for Lever."""
    return f"{lever_region}:{board_slug}" if ats_type == "lever" else board_slug


async def instance_for_company(conn: AsyncConnection, company_id: int) -> int:
    """The id of the instance of the watchlist company's board, created when absent.

    An existing instance of the same board (`ON CONFLICT (source_id, instance_key)`) is returned; when it has
    lost its company (a removed company: `ON DELETE SET NULL`), it is linked to this one.
    """
    company = (await conn.execute(_COMPANY, {"id": company_id})).first()
    if company is None:
        raise LookupError(f"watchlist company {company_id} does not exist")
    board = {
        "source_id": company.ats_type,
        "key": _board_instance_key(company.ats_type, company.board_slug, company.lever_region),
    }
    found = (await conn.execute(_FIND, board)).first()
    if found is None:
        inserted = (await conn.execute(_INSERT, {**board, "company_id": company_id})).scalar()
        if inserted is not None:
            return int(inserted)
        found = (await conn.execute(_FIND, board)).one()  # a concurrent insert won
    if found.watchlist_company_id is None:
        await conn.execute(_LINK, {"id": found.id, "company_id": company_id})
    return int(found.id)
