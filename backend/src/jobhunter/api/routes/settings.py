"""`/api/settings` (spec §6.8, §13.2 to §13.5, §14.2, §14.3; plan Task 16: the dry-run; Task 17: the save
and the reads).

`POST /api/settings/dry-run` (`{payload}`) → 200 `{errors, warnings, projections, effects}`: the preview of a
proposed settings document against the active one (`settings.dryrun.dry_run`). Errors are part of the
preview and come with a 200; the save answers them with a 422. The dry-run runs in a read-only transaction
(`SET TRANSACTION READ ONLY`, the transaction's first statement), where PostgreSQL refuses any write and any
`SELECT … FOR UPDATE | SHARE` (SQLSTATE 25006): it writes nothing and takes no row lock, so a preview never
waits for a settings save or a reservation in progress. `effects` are sorted by name.

`POST /api/settings` (`Idempotency-Key`; `{payload, base_version_id, acknowledged_warnings,
eval_override_ack?}`) → 201 `{version: {id, version_no, effect_classes}, apply_scope_operation_id}`: the save
(`settings.save.save_settings`). A malformed key is refused before any SQL. Then one transaction:
`active_config` `FOR UPDATE` first, then `run_idempotent` around the save, whose own `FOR UPDATE` is then a
no-op. Unlike `POST /api/runs` it never takes `FOR SHARE` first: two tabs holding the shared lock would
deadlock on the upgrade, where this way the second waits and gets 409 `stale_base_version`. The request's
fingerprint takes the acknowledged codes as a set (sorted), so a reordered retry replays. `settings_saved` is
logged after the commit when the save ran (a replay is `run_idempotent`'s `idempotent_replay`). 409 is also
`conflict_state` with detail `schema_dry_run_required` (an unverified model policy, M3 Task 20),
`eval_gate_failed` (§11.13) and `idempotency_in_progress`; 422 is `validation_failed` (the dry-run's errors,
an unacknowledged warning, an unknown query or watchlist company) and `idempotency_key_reused`.

The reads, each a plain `SELECT` (no lock):
- `GET /api/settings` → `{version: {id, version_no, created_at, effect_classes}, payload, pending_scope}`.
  `pending_scope` is `null` without a pending scope, else `{version_id, progress}`: the live `apply_scope`
  operation's `{done, total}` (`0`/`0` before its first chunk), or `null` when no apply is live (one that
  failed for good, until the `scope_apply_failed` reconciler clears the pointer);
- `GET /api/settings/versions?cursor&limit` (limit 1 to 100, default 50) → `{items, next_cursor}`, newest
  first (`version_no DESC`), keyset-paged with §14.3's signed cursor (`api.pagination`: sort `version_no`,
  section `all`, `last_key = [version_no]`, no filters). A cursor this list did not sign is 400
  `invalid_cursor`;
- `GET /api/settings/versions/{id}` → `{version, payload, diff}`, `diff` the `effects.diff_payloads` of the
  previous version's document and this one's (empty for version 1), both read as normal forms. 404
  `not_found` for an unknown id.

The shipped versions (M3 Task 21, §13.3 L4896-4900, §14.2 L5005-5006, Deviation 23):
- `GET /api/settings/rubric-versions` → `[{id, version, content_hash, created_at, active}]` and `GET
  /api/settings/prompt-schema-versions` → `[{id, prompt_schema_hash, created_at, active, dry_run}]`, by id,
  plain reads: `active` is the version `active_config` points at, the hashes are hex, and `dry_run` is the
  version's newest schema dry-run (`{eval_run_id, passed, transport, finished_at}`, by `started_at`; its
  `passed`, `transport` and `finished_at` null while it runs) or `null` without one, so the id to activate is
  discoverable;
- `POST /api/settings/rubric-versions/{id}/activate` and `POST /api/settings/prompt-schema-versions/{id}/
  activate` (`Idempotency-Key`; `{expected_rescore_ack, eval_override_ack?}`) → 201 `{version, llm_tuple_id,
  targets_to_create, bulk_budget_usd}` (Ruling P41): `settings.activate.activate_shipped_version` in one
  transaction, `active_config FOR UPDATE` first, then `run_idempotent` around it, as the save does. 404
  `not_found` for no such version; 409 `conflict_state` for the active one and (detail
  `schema_dry_run_required`) for a model policy without a passed live schema dry-run, `eval_gate_failed`
  and `idempotency_in_progress`; 422 `validation_failed` (the re-score not acknowledged at
  `body.expected_rescore_ack`, an invalid body) and `idempotency_key_reused`. `shipped_version_activated` is
  logged with the ids after the commit, a refusal as `shipped_version_activation_refused` with its code.

`POST /api/settings/scope/retry-apply` (Task 18, §13.4 step 5) → 202 `{operation_id}`: a new `apply_scope`
operation for the active settings version's scope, after a permanent failure cleared the pending pointer. In
one transaction: `active_config` `FOR UPDATE` first (§9.1), then the scope `derive.derived` gives for the
active version's document with the active profile (`ensure_derived`, as a save derives it). It is 409
`conflict_state` when that scope is already the active one, or while any `apply_scope` operation is live
(`scope_apply.live_apply`); otherwise `scope_apply.set_desired_scope` points `pending` at it and requests the
apply. It takes no `Idempotency-Key` (§14.1 does not list it): a repeated request finds the apply live and is
refused. `scope_apply_retry_requested` is logged with the ids after the commit, a refusal as
`scope_apply_retry_refused` with its reason code.

`utcnow` is a name of this module, so a test injects the clock by patching
`jobhunter.api.routes.settings.utcnow`; the save takes its `now` from it too. The log lines hold ids, counts,
warning codes and effect classes only, never a value of the payload.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.responses import JSONResponse, Response

from jobhunter.api.deps import get_config, get_engine, require_session
from jobhunter.api.errors import Problem, as_problem
from jobhunter.api.models import DecimalOut, UtcDatetime
from jobhunter.api.openapi import BODY_LIMIT_RESPONSES, CSRF_RESPONSES, SESSION_RESPONSES
from jobhunter.api.pagination import (
    CURSOR_VERSION,
    SECTION_ALL,
    cursor_key,
    decode_cursor,
    encode_cursor,
    filters_hash,
    invalid_cursor,
)
from jobhunter.api_idempotency import (
    IDEMPOTENCY_HEADER,
    StoredResponse,
    check_idempotency_key,
    request_fingerprint,
    run_idempotent,
)
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.errors import Refusal
from jobhunter.observability.logging import get_logger
from jobhunter.settings import scope_apply
from jobhunter.settings.activate import (
    ActivateRequest,
    ShippedActivation,
    ShippedKind,
    activate_shipped_version,
)
from jobhunter.settings.active import lock_active
from jobhunter.settings.derive import active_profile, derived, ensure_derived
from jobhunter.settings.dryrun import active_settings, dry_run
from jobhunter.settings.effects import EffectClass, diff_payloads
from jobhunter.settings.model import Settings
from jobhunter.settings.save import SCHEMA_DRY_RUN_REQUIRED, SaveOutcome, SaveRequest, save_settings
from jobhunter.settings.scope_apply import KIND as APPLY_SCOPE
from jobhunter.settings.validate import IssueCode
from jobhunter.types import JSONValue
from jobhunter.worker.tasks import defer_in_tx

router = APIRouter(
    prefix="/api/settings", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES
)
log = get_logger("jobhunter.api.settings")

SAVE_ROUTE: Final = "settings.save"
VERSIONS_SORT: Final = "version_no"
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 100
_BIGINT_MAX: Final = 2**63 - 1
# A warning code as the dry-run names them; the save refuses any other string.
_WARNING_CODE: Final = r"^[a-z][a-z0-9_]{0,63}$"
_MAX_ACKNOWLEDGED: Final = 64

_READ_ONLY: Final = q("SET TRANSACTION READ ONLY")
_LOCK_ACTIVE_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_ACTIVE: Final = q(
    "SELECT v.id, v.version_no, v.created_at, v.effect_classes, v.payload, a.pending_scope_version_id, "
    "o.id AS operation_id, o.progress FROM active_config a "
    "JOIN settings_versions v ON v.id = a.settings_version_id "
    "LEFT JOIN operations o ON a.pending_scope_version_id IS NOT NULL AND o.kind = :kind "
    "AND o.dedup_key = :kind || ':' || a.pending_scope_version_id AND o.status IN ('pending', 'running') "
    "WHERE a.id = 1"
)
_VERSIONS: Final = q(
    "SELECT id, version_no, created_at, effect_classes FROM settings_versions "
    "WHERE CAST(:after AS integer) IS NULL OR version_no < CAST(:after AS integer) "
    "ORDER BY version_no DESC LIMIT :limit"
)
_VERSION: Final = q(
    "SELECT id, version_no, created_at, effect_classes, payload FROM settings_versions WHERE id = :id"
)
_PREVIOUS: Final = q("SELECT payload FROM settings_versions WHERE version_no = :no")
_RUBRIC_VERSIONS: Final = q(
    "SELECT r.id, r.version, r.content_hash, r.created_at, r.id = a.rubric_version_id AS active "
    "FROM rubric_versions r CROSS JOIN active_config a WHERE a.id = 1 ORDER BY r.id"
)
# Each version with its newest schema dry-run (by `started_at`, then id), whatever its end.
_PROMPT_SCHEMA_VERSIONS: Final = q(
    "SELECT p.id, p.prompt_schema_hash, p.created_at, p.id = a.prompt_schema_version_id AS active, "
    "d.id AS eval_run_id, d.passed, d.metrics ->> 'transport' AS transport, d.finished_at "
    "FROM prompt_schema_versions p CROSS JOIN active_config a LEFT JOIN LATERAL (SELECT e.id, e.passed, "
    "e.metrics, e.finished_at FROM eval_runs e WHERE e.kind = 'schema_dry_run' "
    "AND e.prompt_schema_version_id = p.id ORDER BY e.started_at DESC, e.id DESC LIMIT 1) d ON true "
    "WHERE a.id = 1 ORDER BY p.id"
)
ACTIVATE_ROUTES: Final[dict[ShippedKind, str]] = {
    "rubric": "settings.rubric_versions.activate",
    "prompt_schema": "settings.prompt_schema_versions.activate",
}
_NEVER: Final = (
    "null means never: with no Adzuna allowance (a monthly cap of 1 leaves a bucket of 0 units) no cell is "
    "ever refreshed"
)


class DryRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: JsonValue


class FieldIssueOut(BaseModel):
    """A refused field (`settings.validate.FieldIssue`): its path from `payload`, a code and a message that
    never repeats the value."""

    field: str
    code: IssueCode
    message: str


class WarningOut(BaseModel):
    """A warning a save must acknowledge by its `code` (§13.2), with its details."""

    code: str
    detail: dict[str, JsonValue] = Field(
        description=f"The warning's details. In `adzuna_rotation`, `cadence_days` is days or null; {_NEVER}."
    )


class DryRunOut(BaseModel):
    errors: list[FieldIssueOut]
    warnings: list[WarningOut]
    projections: dict[str, JsonValue] = Field(
        description=f"The preview by part (§13.5). In `adzuna`, `cadence_days` is days or null; {_NEVER}."
    )
    effects: list[EffectClass]


class VersionOut(BaseModel):
    """A settings version as the lists show it; `created_at` in UTC, `effect_classes` sorted."""

    id: int
    version_no: int
    created_at: datetime
    effect_classes: list[EffectClass]

    @field_validator("created_at")
    @classmethod
    def _in_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ProgressOut(BaseModel):
    done: int
    total: int


class PendingScopeOut(BaseModel):
    """The scope being applied (§13.4) and its live apply's progress; `progress` is `null` while no apply is
    live (one that failed for good, until the pointer is cleared)."""

    version_id: int
    progress: ProgressOut | None


class SettingsOut(BaseModel):
    version: VersionOut
    payload: dict[str, JsonValue]
    pending_scope: PendingScopeOut | None


WarningCode = Annotated[str, StringConstraints(pattern=_WARNING_CODE)]


class SaveIn(BaseModel):
    """A save (§13.2): `acknowledged_warnings` must cover every warning code the dry-run of `payload` gives;
    `eval_override_ack` acknowledges a failed eval gate (§11.13)."""

    model_config = ConfigDict(extra="forbid")

    payload: JsonValue
    base_version_id: int = Field(ge=1, le=_BIGINT_MAX)
    acknowledged_warnings: list[WarningCode] = Field(max_length=_MAX_ACKNOWLEDGED)
    eval_override_ack: bool = False


class SavedVersionOut(BaseModel):
    id: int
    version_no: int
    effect_classes: list[EffectClass]


class SaveOut(BaseModel):
    version: SavedVersionOut
    apply_scope_operation_id: int | None = Field(
        description="The `apply_scope` operation the version's scope needs (§13.4), or null when none does"
    )


class VersionsPageOut(BaseModel):
    items: list[VersionOut]
    next_cursor: str | None


class DiffEntryOut(BaseModel):
    """One changed leaf at its path (`payload.…`); `before` or `after` is null where the value is absent."""

    path: str
    before: JsonValue
    after: JsonValue


class VersionDetailOut(BaseModel):
    version: VersionOut
    payload: dict[str, JsonValue]
    diff: list[DiffEntryOut]


class RetryApplyOut(BaseModel):
    operation_id: int = Field(description="The new `apply_scope` operation of the active version's scope")


class RubricVersionOut(BaseModel):
    """A shipped rubric version; `content_hash` in hex, `active` when `active_config` points at it."""

    id: int
    version: str
    content_hash: str
    created_at: UtcDatetime
    active: bool


class SchemaDryRunOut(BaseModel):
    """A schema dry-run of a prompt-schema version (§10.11): `passed`, `transport` (`live` or `fake`) and
    `finished_at` are null while it runs."""

    eval_run_id: int
    passed: bool | None
    transport: str | None
    finished_at: UtcDatetime | None


class PromptSchemaVersionOut(BaseModel):
    """A shipped prompt-schema version, its hash in hex, whether it is active, and its newest schema
    dry-run (`null` without one)."""

    id: int
    prompt_schema_hash: str
    created_at: UtcDatetime
    active: bool
    dry_run: SchemaDryRunOut | None


class ActivateIn(BaseModel):
    """An activation (§14.2): `expected_rescore_ack` acknowledges the re-score of the retained ads (required
    while there are any), `eval_override_ack` a failed eval gate (§11.13)."""

    model_config = ConfigDict(extra="forbid")

    expected_rescore_ack: bool
    eval_override_ack: bool = False


class ActivatedOut(BaseModel):
    version: int = Field(description="The number of the settings version the activation saved")
    llm_tuple_id: int | None = Field(description="The tuple active after it; null while no profile is active")
    targets_to_create: int = Field(description="The retained ads the new tuple re-scores")
    bulk_budget_usd: DecimalOut = Field(description="The budget of the re-score's bulk operation (§6.9.3)")


VersionId = Annotated[int, Path(ge=1, le=_BIGINT_MAX, description="The id of a settings version")]
ShippedVersionId = Annotated[
    int, Path(ge=1, le=_BIGINT_MAX, description="The id of a shipped rubric or prompt-schema version")
]


@router.post(
    "/dry-run",
    response_model=DryRunOut,
    responses={
        422: {"description": "Invalid body (`validation_failed`)"},
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def settings_dry_run(body: DryRunIn, engine: Annotated[AsyncEngine, Depends(get_engine)]) -> DryRunOut:
    """Validate and preview a proposed settings document: its errors, warnings, projections and effects."""
    async with engine.connect() as conn:  # rolled back when the block ends
        await conn.execute(_READ_ONLY)
        result = await dry_run(conn, body.payload, active=await active_settings(conn), now=utcnow())
    log.info(
        "settings_dry_run",
        errors=len(result.errors),
        warnings=[w.code for w in result.warnings],
        effects=sorted(result.effects),
    )
    return DryRunOut(
        errors=[FieldIssueOut(field=e.field, code=e.code, message=e.message) for e in result.errors],
        warnings=[WarningOut(code=w.code, detail=dict(w.detail)) for w in result.warnings],
        projections=result.projections,
        effects=sorted(result.effects),
    )


def _progress(progress: JSONValue) -> ProgressOut:
    """`operations.progress` as `{done, total}`; a count the apply has not reported yet is 0."""
    counts = progress if isinstance(progress, dict) else {}

    def count(name: str) -> int:
        value = counts.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return ProgressOut(done=count("done"), total=count("total"))


@router.get("", response_model=SettingsOut)
async def get_settings(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> SettingsOut:
    """The active settings version, its document and the scope being applied, if any."""
    async with engine.connect() as conn:
        row = (await conn.execute(_ACTIVE, {"kind": APPLY_SCOPE})).one()
    pending = None
    if row.pending_scope_version_id is not None:
        progress = None if row.operation_id is None else _progress(row.progress)
        pending = PendingScopeOut(version_id=row.pending_scope_version_id, progress=progress)
    return SettingsOut(
        version=VersionOut.model_validate(row, from_attributes=True),
        payload=row.payload,
        pending_scope=pending,
    )


def _saved(outcome: SaveOutcome) -> SaveOut:
    return SaveOut(
        version=SavedVersionOut(
            id=outcome.version_id, version_no=outcome.version_no, effect_classes=sorted(outcome.effects)
        ),
        apply_scope_operation_id=outcome.apply_scope_operation_id,
    )


@router.post(
    "",
    status_code=201,
    response_model=SaveOut,
    responses={
        400: {"description": "Idempotency-Key missing or malformed (`idempotency_key_required`)"},
        409: {
            "description": "The base is not the active version (`stale_base_version`), the model policy has "
            "no passed live schema dry-run of a (schema, model) pair it needs (`conflict_state`, detail "
            "`schema_dry_run_required`, whatever the override), the eval gate failed without the override "
            "(`eval_gate_failed`), or a request with this key is still running (`idempotency_in_progress`)"
        },
        422: {
            "description": "Invalid body or settings, an unacknowledged warning (`validation_failed`), or "
            "the key was used for another request (`idempotency_key_reused`)"
        },
        **CSRF_RESPONSES,
        **BODY_LIMIT_RESPONSES,
    },
)
async def save_settings_version(
    body: SaveIn,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Save the document as a new settings version and activate it (201)."""
    key = check_idempotency_key(idempotency_key)  # a malformed key costs no database round trip
    now = utcnow()
    acknowledged = frozenset(body.acknowledged_warnings)
    request = SaveRequest(
        payload=body.payload,
        base_version_id=body.base_version_id,
        acknowledged_warnings=acknowledged,
        eval_override_ack=body.eval_override_ack,
    )
    codes: list[JSONValue] = [*sorted(acknowledged)]  # a set: a reordered retry is the same request
    fingerprint = request_fingerprint({**body.model_dump(mode="json"), "acknowledged_warnings": codes}, {})

    saved: list[SaveOut] = []  # the handler's outcome; empty on a replay of the stored response

    async def handler(conn: AsyncConnection) -> StoredResponse:
        outcome = _saved(await save_settings(conn, request, now=now, defer=defer_in_tx))
        saved.append(outcome)
        return StoredResponse(201, outcome.model_dump(mode="json"))

    try:
        async with engine.begin() as conn:
            await conn.execute(_LOCK_ACTIVE_CONFIG)  # the first lock (§9.1), before the key's row
            stored = await run_idempotent(
                conn, route=SAVE_ROUTE, key=key, fingerprint=fingerprint, now=now, handler=handler
            )
    except (Problem, Refusal) as refused:  # counts and codes: a field path can hold a key the sender typed
        problem = as_problem(refused)
        log.info(
            "settings_save_refused",
            code=problem.code,
            errors=len(problem.errors or []),
            issue_codes=sorted({e.code for e in problem.errors or []}),
        )
        raise
    for outcome in saved:  # after the commit; a replay is logged by `run_idempotent` (`idempotent_replay`)
        log.info(
            "settings_saved",
            version_id=outcome.version.id,
            version_no=outcome.version.version_no,
            effects=outcome.version.effect_classes,
            apply_scope_operation_id=outcome.apply_scope_operation_id,
        )
    return JSONResponse(stored.body, status_code=stored.status)


def _version_after(cursor: str | None, key: bytes) -> int | None:
    """The `version_no` the next page starts below, from a cursor of this list; `None` for the first page."""
    if cursor is None:
        return None
    body = decode_cursor(cursor, key=key, sort=VERSIONS_SORT, filters_hash=filters_hash({}))
    last_key = body.get("last_key")
    if body.get("section") != SECTION_ALL or body.get("dir") != "desc" or not isinstance(last_key, list):
        raise invalid_cursor()
    match last_key:
        case [int(version_no)] if not isinstance(version_no, bool) and version_no >= 1:
            return version_no
    raise invalid_cursor()


@router.get(
    "/versions",
    response_model=VersionsPageOut,
    responses={400: {"description": "A cursor this list did not issue (`invalid_cursor`)"}},
)
async def list_versions(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    cfg: Annotated[AppConfig, Depends(get_config)],
    cursor: Annotated[
        str | None, Query(max_length=2048, description="`next_cursor` of the page before")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> VersionsPageOut:
    """The settings versions, newest first, keyset-paged (§14.3)."""
    key = cursor_key(cfg)
    after = _version_after(cursor, key)
    async with engine.connect() as conn:
        rows = (await conn.execute(_VERSIONS, {"after": after, "limit": limit + 1})).all()
    items = [VersionOut.model_validate(row, from_attributes=True) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(
            {
                "v": CURSOR_VERSION,
                "sort": VERSIONS_SORT,
                "dir": "desc",
                "section": SECTION_ALL,
                "last_key": [items[-1].version_no],
                "filters_hash": filters_hash({}),
            },
            key=key,
        )
    return VersionsPageOut(items=items, next_cursor=next_cursor)


@router.get(
    "/versions/{version_id}",
    response_model=VersionDetailOut,
    responses={404: {"description": "No such settings version (`not_found`)"}},
)
async def get_version(
    version_id: VersionId, engine: Annotated[AsyncEngine, Depends(get_engine)]
) -> VersionDetailOut:
    """A settings version, its document and its diff against the previous version."""
    async with engine.connect() as conn:
        row = (await conn.execute(_VERSION, {"id": version_id})).first()
        if row is None:
            raise Problem(404, "not_found", "Settings version not found")
        previous = (await conn.execute(_PREVIOUS, {"no": row.version_no - 1})).scalar_one_or_none()
    payload = Settings.model_validate(row.payload).to_payload()
    diff = [] if previous is None else diff_payloads(Settings.model_validate(previous).to_payload(), payload)
    return VersionDetailOut(
        version=VersionOut.model_validate(row, from_attributes=True),
        payload=payload,
        diff=[DiffEntryOut(path=e.path, before=e.before, after=e.after) for e in diff],
    )


@router.get("/rubric-versions", response_model=list[RubricVersionOut])
async def list_rubric_versions(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> list[RubricVersionOut]:
    """The shipped rubric versions, by id, the active one marked."""
    async with engine.connect() as conn:
        rows = (await conn.execute(_RUBRIC_VERSIONS)).all()
    return [
        RubricVersionOut(
            id=row.id,
            version=row.version,
            content_hash=bytes(row.content_hash).hex(),
            created_at=row.created_at,
            active=row.active,
        )
        for row in rows
    ]


@router.get("/prompt-schema-versions", response_model=list[PromptSchemaVersionOut])
async def list_prompt_schema_versions(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
) -> list[PromptSchemaVersionOut]:
    """The shipped prompt-schema versions, by id, the active one marked, each with its newest schema
    dry-run."""
    async with engine.connect() as conn:
        rows = (await conn.execute(_PROMPT_SCHEMA_VERSIONS)).all()
    return [
        PromptSchemaVersionOut(
            id=row.id,
            prompt_schema_hash=bytes(row.prompt_schema_hash).hex(),
            created_at=row.created_at,
            active=row.active,
            dry_run=None
            if row.eval_run_id is None
            else SchemaDryRunOut(
                eval_run_id=row.eval_run_id,
                passed=row.passed,
                transport=row.transport,
                finished_at=row.finished_at,
            ),
        )
        for row in rows
    ]


_ACTIVATE_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    400: {"description": "Idempotency-Key missing or malformed (`idempotency_key_required`)"},
    404: {"description": "No such version (`not_found`)"},
    409: {
        "description": "The version is the active one (`conflict_state`), the active model policy has no "
        "passed live schema dry-run of a (schema, model) pair it needs for the prompt-schema version the "
        "activation leaves active (`conflict_state`, detail `schema_dry_run_required`, whatever the "
        "override), the eval gate of the candidate failed without the override (`eval_gate_failed`), or a "
        "request with this key is still running (`idempotency_in_progress`)"
    },
    422: {
        "description": "Invalid body, the re-score of retained ads not acknowledged (`validation_failed` at "
        "`body.expected_rescore_ack`), or the key was used for another request (`idempotency_key_reused`)"
    },
    **CSRF_RESPONSES,
    **BODY_LIMIT_RESPONSES,
}


async def _activate(
    kind: ShippedKind, version_id: int, body: ActivateIn, engine: AsyncEngine, idempotency_key: str | None
) -> Response:
    """The activation's transaction (module docstring): `active_config FOR UPDATE`, then `run_idempotent`."""
    key = check_idempotency_key(idempotency_key)  # a malformed key costs no database round trip
    now = utcnow()
    request = ActivateRequest(
        expected_rescore_ack=body.expected_rescore_ack, eval_override_ack=body.eval_override_ack
    )
    done: list[ShippedActivation] = []  # the handler's activation; empty on a replay of the stored response

    async def handler(conn: AsyncConnection) -> StoredResponse:
        activation = await activate_shipped_version(
            conn, kind=kind, version_id=version_id, req=request, now=now, defer=defer_in_tx
        )
        done.append(activation)
        return StoredResponse(201, activation.body())

    try:
        async with engine.begin() as conn:
            await conn.execute(_LOCK_ACTIVE_CONFIG)  # the first lock (§9.1), before the key's row
            stored = await run_idempotent(
                conn,
                route=ACTIVATE_ROUTES[kind],
                key=key,
                fingerprint=request_fingerprint(body.model_dump(mode="json"), {"version_id": version_id}),
                now=now,
                handler=handler,
            )
    except (Problem, Refusal) as refused:
        problem = as_problem(refused)
        log.info(
            "shipped_version_activation_refused",
            kind=kind,
            shipped_version_id=version_id,
            code=problem.code,
            detail_code=problem.detail if problem.detail == SCHEMA_DRY_RUN_REQUIRED else None,
        )
        raise
    for activation in done:  # after the commit; a replay is logged by `run_idempotent`
        log.info(
            "shipped_version_activated",
            kind=kind,
            shipped_version_id=version_id,
            version_id=activation.saved.version_id,
            version_no=activation.saved.version_no,
            llm_tuple_id=activation.saved.llm_tuple_id,
            targets_to_create=activation.targets_to_create,
        )
    return JSONResponse(stored.body, status_code=stored.status)


@router.post(
    "/rubric-versions/{version_id}/activate",
    status_code=201,
    response_model=ActivatedOut,
    responses=_ACTIVATE_RESPONSES,
)
async def activate_rubric_version(
    version_id: ShippedVersionId,
    body: ActivateIn,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Activate a shipped rubric version: an `llm_tuple` change (§13.3), its re-score acknowledged (201)."""
    return await _activate("rubric", version_id, body, engine, idempotency_key)


@router.post(
    "/prompt-schema-versions/{version_id}/activate",
    status_code=201,
    response_model=ActivatedOut,
    responses=_ACTIVATE_RESPONSES,
)
async def activate_prompt_schema_version(
    version_id: ShippedVersionId,
    body: ActivateIn,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> Response:
    """Activate a shipped prompt-schema version: an `llm_tuple` change (§13.3) that needs a passed live schema
    dry-run of every (schema, model) pair of the active model policy (§10.11) (201)."""
    return await _activate("prompt_schema", version_id, body, engine, idempotency_key)


def _retry_refused(reason: str, detail: str) -> Problem:
    log.info("scope_apply_retry_refused", reason=reason)
    return Problem(409, "conflict_state", "The scope cannot be applied again now", detail=detail)


@router.post(
    "/scope/retry-apply",
    status_code=202,
    response_model=RetryApplyOut,
    responses={
        409: {
            "description": "The active settings version's scope is already the active scope, or an "
            "`apply_scope` operation is live (`conflict_state`)"
        },
        **CSRF_RESPONSES,
    },
)
async def retry_scope_apply(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> RetryApplyOut:
    """Apply the active settings version's scope again, after its apply failed for good (202)."""
    now = utcnow()
    async with engine.begin() as conn:
        active = await lock_active(conn)  # the first lock (§9.1), then the active document
        profile = await active_profile(conn, active.profile_version_id)
        ids = await ensure_derived(conn, derived(Settings.model_validate(active.payload), profile=profile))
        scope_version_id = ids.scope_version_id
        if scope_version_id == active.scope_version_id:
            raise _retry_refused("scope_active", "the active settings version's scope is the active scope")
        if await scope_apply.live_apply(conn) is not None:
            raise _retry_refused("apply_live", "a scope is being applied; wait for it to end")
        operation_id = await scope_apply.set_desired_scope(conn, scope_version_id, now=now, defer=defer_in_tx)
        if operation_id is None:  # only for the active scope, refused above
            raise RuntimeError("set_desired_scope requested no apply for a scope that is not the active one")
    log.info("scope_apply_retry_requested", operation_id=operation_id, scope_version_id=scope_version_id)
    return RetryApplyOut(operation_id=operation_id)
