"""The activation of a shipped rubric or prompt-schema version (spec §13.3 L4896-4900, §14.2 L5005-5006,
§10.11, §11.13; plan Task 21, Deviation 23, M2 Deviation 42, Codex r1 #25, #35, r2 B7, Ruling P41).

A shipped version never activates itself (§13.3); the user activates it, and that is an `llm_tuple` change:
the tuple's rubric or prompt-schema pointer moves, so the profile's tuple is a new one, whose targets
`create_targets_for_tuple` creates. `activate_shipped_version(conn, *, kind, version_id, req, now, defer)`
runs in the caller's transaction, which already holds `active_config FOR UPDATE` and the idempotency row (the
route: `api.routes.settings`), in this order (Codex r1 #35):

1. `active_config FOR UPDATE`, a statement of its own (`settings.active`'s rule), a no-op under the route's
   lock;
2. the idempotency row (the route's, between 1 and 3);
3. the version row: no such version is 404 `not_found`; the active one is 409 `conflict_state` (an
   activation of it would change nothing but add a settings version);
4. for a prompt-schema version, `validate.schema_verified(prompt_schema_version_id=<it>,
   model_policy_id=<the active one>)` (M3 Task 11: live, per version and per model, over every run, never
   "the latest run"): false is 409 `conflict_state` with `detail = "schema_dry_run_required"` and a title
   naming the pairs without a pass (`save.schema_dry_run_required`), whatever the override. A process on the
   fake LLM skips it, as the save and the score lane's guard do (`validate.fake_llm_process`): no live run
   exists there;
5. the eval gate of the candidate, the active configuration's with the new pointer (`derive.candidate_of`
   with the field replaced; M3 Task 20): `failed` without `eval_override_ack` is 409 `eval_gate_failed`;
6. the re-score estimate (`profile.estimate.rescore_estimate`, plain reads): with an active profile, the
   retained current revisions are what `create_targets_for_tuple` re-scores, and while there are any
   `expected_rescore_ack` is required, else 422 `validation_failed` on `body.expected_rescore_ack`
   (`ack_required`, the confirm's code; M2 Deviation 42). Without an active profile no tuple exists
   (`active_config` pairs them), nothing is re-scored, and nothing needs acknowledging;
7. the pointer flip (`UPDATE active_config SET rubric_version_id | prompt_schema_version_id = :id`);
8. the internal save of the active document (`save.save_settings(..., internal=True,
   forced_effects={"llm_tuple"})`, its base the active version, its acknowledgements the dry-run's warning
   codes of the document, the override passed on): the version row records `effect_classes = ["llm_tuple"]`
   and the override; `_apply_llm_tuple` runs `ensure_tuple` on the flipped pointers and flips the tuple in
   `_FLIP`; the tuple activation (`targets.activate_tuple`: the month row, the bulk operations, the
   cancellations, `create_targets_for_tuple`) runs after `_FLIP`, and `ranking.bump` last (Codex r2 B7). The
   save checks the gate again, on the same candidate, and an `llm_tuple` save's model policy against the
   prompt-schema version the activation leaves active: a rubric activation is refused (409 `conflict_state`,
   `schema_dry_run_required`) while the active policy is unverified for it, the state in which the score
   lane is paused anyway (`cycles.pause_reason`'s `schema_unverified`).

The activation's own refusals (steps 3 to 6) come before its first write. The save's re-checks of step 8 come
after the step-7 flip and can still refuse (the model-policy check of a rubric activation above; a dry-run
error of the active document): the save raises, and the route's transaction rolls back with the flip and the
idempotency row, so nothing of the activation remains and a retry with the key runs again.

The outcome (`ShippedActivation`) is §14.2's 201 body (Ruling P41): `{version, llm_tuple_id,
targets_to_create, bulk_budget_usd}`, `version` the new settings version's number, `targets_to_create` the
estimate's count and `bulk_budget_usd` the estimate's figure (both 0 without an active profile: no tuple, no
bulk operation). Every refusal is a `jobhunter.errors.Refusal` (M4 Task 1, R6), which the api answers with its
problem. This module does not log: the route logs the ids after its commit.

**The preview** (`activation_preview(conn, *, kind, version_id, now)`, plan M4 Task 14, D8) is what steps 3
to 6 read, with no refusal but the 404 and no write: the route runs it in one read snapshot
(`db.snapshot.read_snapshot`, read-only, so PostgreSQL refuses any write and any row lock), with a plain read
of `active_config` in place of step 1's lock. `_checks` is the one computation of steps 4 to 6 that the
activation and the preview share: the active document, the prompt-schema version's verification of the
active model policy (`None` where step 4 checks nothing: a rubric version, or a process on the fake LLM) with
the pairs it lacks, the candidate's gate, and the re-score estimate (`None` without an active profile). The
preview adds whether the version is the active one (step 3's 409), the run the candidate's gate reads once
it is calibrated (`validate.gate_run`; shown below 30 cases too), the models of the unverified pairs, and the
estimate's figures, all 0 without an active profile, as the activation's body has them.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Literal

from pydantic import BaseModel, Field
from sqlalchemy import TextClause
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.models import DecimalOut, UtcDatetime
from jobhunter.db.sql import q
from jobhunter.errors import Refusal, RefusalField
from jobhunter.llm.budget import USD_QUANTUM
from jobhunter.profile.activation import ACK_FIELD, ACK_REQUIRED
from jobhunter.profile.estimate import RescoreEstimate, rescore_estimate
from jobhunter.settings.derive import candidate_of
from jobhunter.settings.dryrun import dry_run
from jobhunter.settings.effects import EffectClass
from jobhunter.settings.model import Settings
from jobhunter.settings.save import (
    SaveOutcome,
    SaveRequest,
    eval_gate_failed,
    save_settings,
    schema_dry_run_required,
)
from jobhunter.settings.validate import (
    Candidate,
    GateState,
    eval_gate_state,
    fake_llm_process,
    gate_run,
    schema_verified,
    unverified_pairs,
)
from jobhunter.types import JSONValue
from jobhunter.worker.tasks import Deferrer

ShippedKind = Literal["rubric", "prompt_schema"]
FORCED: Final[frozenset[EffectClass]] = frozenset({"llm_tuple"})
_NO_USD: Final = Decimal(0).quantize(USD_QUANTUM)

_ACTIVE_SQL: Final = (
    "SELECT settings_version_id, profile_version_id, model_policy_id, rubric_version_id, "
    "prompt_schema_version_id FROM active_config WHERE id = 1"
)
_LOCK_ACTIVE: Final = q(_ACTIVE_SQL + " FOR UPDATE")
_ACTIVE: Final = q(_ACTIVE_SQL)  # the preview's plain read
_PAYLOAD: Final = q("SELECT payload FROM settings_versions WHERE id = :id")


@dataclass(frozen=True)
class _Shipped:
    """One kind of shipped version: its name, its `active_config` pointer, and its two statements."""

    name: str
    pointer: Literal["rubric_version_id", "prompt_schema_version_id"]
    find: TextClause
    flip: TextClause


_SHIPPED: Final[Mapping[ShippedKind, _Shipped]] = MappingProxyType(
    {
        "rubric": _Shipped(
            "Rubric version",
            "rubric_version_id",
            q("SELECT 1 FROM rubric_versions WHERE id = :id"),
            q("UPDATE active_config SET rubric_version_id = :id, updated_at = :now WHERE id = 1"),
        ),
        "prompt_schema": _Shipped(
            "Prompt-schema version",
            "prompt_schema_version_id",
            q("SELECT 1 FROM prompt_schema_versions WHERE id = :id"),
            q("UPDATE active_config SET prompt_schema_version_id = :id, updated_at = :now WHERE id = 1"),
        ),
    }
)


@dataclass(frozen=True)
class ActivateRequest:
    """The user's acknowledgements: the re-score's cost (required while retained revisions exist) and the
    eval gate's override (§11.13)."""

    expected_rescore_ack: bool
    eval_override_ack: bool = False


@dataclass(frozen=True)
class ShippedActivation:
    """An activation done: the save it made, and what it re-scores at what bulk budget (module docstring)."""

    saved: SaveOutcome
    targets_to_create: int
    bulk_budget_usd: Decimal

    def body(self) -> dict[str, JSONValue]:
        """§14.2's 201 body (Ruling P41), the amount as a decimal string."""
        return {
            "version": self.saved.version_no,
            "llm_tuple_id": self.saved.llm_tuple_id,
            "targets_to_create": self.targets_to_create,
            "bulk_budget_usd": format(self.bulk_budget_usd, "f"),
        }


@dataclass(frozen=True)
class _Checks:
    """What steps 4 to 6 read (module docstring, the preview): the active document, the prompt-schema
    version's verification (`None`: not checked) with the pairs it lacks, the candidate and its gate, and the
    re-score estimate (`None`: no active profile)."""

    payload: JSONValue
    settings: Settings
    schema_verified: bool | None
    unverified: list[tuple[str, str]]
    candidate: Candidate
    gate_state: GateState
    estimate: RescoreEstimate | None


async def _found(conn: AsyncConnection, kind: ShippedKind, version_id: int) -> None:
    """Step 3's 404 `not_found` for no such version."""
    shipped = _SHIPPED[kind]
    if (await conn.execute(shipped.find, {"id": version_id})).first() is None:
        raise Refusal(404, "not_found", f"{shipped.name} not found")


async def _checks(
    conn: AsyncConnection, *, kind: ShippedKind, version_id: int, active: Row[Any], now: datetime
) -> _Checks:
    """Steps 4 to 6's reads for `active` (the `active_config` row), refusing nothing (module docstring)."""
    payload = (await conn.execute(_PAYLOAD, {"id": active.settings_version_id})).scalar_one()
    settings = Settings.model_validate(payload)
    verified: bool | None = None
    unverified: list[tuple[str, str]] = []
    if kind == "prompt_schema" and not fake_llm_process():  # step 4
        verified = await schema_verified(
            conn, prompt_schema_version_id=version_id, model_policy_id=int(active.model_policy_id)
        )
        if not verified:
            unverified = await unverified_pairs(
                conn, prompt_schema_version_id=version_id, model_policy=settings.scoring.model_policy
            )
    candidate = await candidate_of(conn, settings)  # step 5: the active one with the new pointer
    if kind == "rubric":
        candidate = replace(candidate, rubric_version_id=version_id)
    else:
        candidate = replace(candidate, prompt_schema_version_id=version_id)
    gate = await eval_gate_state(conn, candidate=candidate)
    estimate = None
    if active.profile_version_id is not None:  # step 6: without a profile no tuple re-scores anything
        estimate = await rescore_estimate(conn, settings, now=now)
    return _Checks(payload, settings, verified, unverified, candidate, gate, estimate)


async def activate_shipped_version(
    conn: AsyncConnection,
    *,
    kind: ShippedKind,
    version_id: int,
    req: ActivateRequest,
    now: datetime,
    defer: Deferrer,
) -> ShippedActivation:
    """Activate the shipped `kind` version `version_id` in the caller's transaction (module docstring)."""
    active = (await conn.execute(_LOCK_ACTIVE)).one()  # step 1
    shipped = _SHIPPED[kind]
    await _found(conn, kind, version_id)  # step 3
    if getattr(active, shipped.pointer) == version_id:
        raise Refusal(
            409,
            "conflict_state",
            f"{shipped.name} {version_id} is the active one",
            detail="activate another version; this one is in use already",
        )
    checks = await _checks(conn, kind=kind, version_id=version_id, active=active, now=now)
    if checks.schema_verified is False:  # step 4
        raise schema_dry_run_required(checks.unverified)
    if checks.gate_state == "failed" and not req.eval_override_ack:
        raise eval_gate_failed()  # step 5
    targets, bulk_budget_usd = 0, _NO_USD
    if checks.estimate is not None:  # step 6
        targets, bulk_budget_usd = checks.estimate.targets, checks.estimate.bulk_budget_usd
    payload = checks.payload
    if targets > 0 and not req.expected_rescore_ack:
        message = f"Activating re-scores {targets} retained ads; acknowledge the estimate to activate."
        raise Refusal(
            422,
            "validation_failed",
            "The re-score is not acknowledged",
            fields=[RefusalField(field=ACK_FIELD, code=ACK_REQUIRED, message=message)],
        )
    await conn.execute(shipped.flip, {"id": version_id, "now": now})  # step 7
    warnings = (await dry_run(conn, payload, active=checks.settings, now=now)).warnings
    saved = await save_settings(  # step 8
        conn,
        SaveRequest(
            payload=payload,
            base_version_id=int(active.settings_version_id),
            acknowledged_warnings=frozenset(w.code for w in warnings),
            eval_override_ack=req.eval_override_ack,
        ),
        now=now,
        defer=defer,
        internal=True,
        forced_effects=FORCED,
    )
    return ShippedActivation(saved, targets_to_create=targets, bulk_budget_usd=bulk_budget_usd)


# ---- the preview (module docstring) ------------------------------------------------------------------------


class GoldenRunOut(BaseModel):
    """The golden run that decides the candidate's eval gate once it is calibrated: the newest completed run
    of the candidate on the production route with an accepted transport."""

    eval_run_id: int
    passed: bool | None = Field(description="Whether it passed; null when it was never gated")
    spearman_rho: DecimalOut | None = Field(
        description="Spearman's rho of score and label; null when undefined"
    )
    finished_at: UtcDatetime | None


class ActivationPreviewOut(BaseModel):
    """What activating the version would check and re-score, read without writing anything."""

    version_id: int
    active: bool = Field(description="The version is the active one, which cannot be activated again")
    gate_state: GateState = Field(
        description="The eval gate of the candidate, the active configuration with this version"
    )
    latest_golden_run: GoldenRunOut | None
    schema_verified: bool | None = Field(
        description="Whether passed live schema dry-runs of this prompt-schema version cover every model of "
        "the active model policy; null where the activation checks none (a rubric version, or the fake LLM)"
    )
    unverified_models: list[str] = Field(
        description="The models without a passed live schema dry-run of a schema they need, sorted"
    )
    targets: int = Field(
        description="The retained ads the activation re-scores; 0 while no profile is active"
    )
    est_usd_online: DecimalOut = Field(description="The re-score's estimated cost online")
    est_usd_batch: DecimalOut = Field(description="The re-score's estimated cost in batches")
    bulk_budget_usd: DecimalOut = Field(description="The budget of the re-score's bulk operation")


async def activation_preview(
    conn: AsyncConnection, *, kind: ShippedKind, version_id: int, now: datetime
) -> ActivationPreviewOut:
    """What activating the shipped `kind` version `version_id` reads before its pointer flip, on the caller's
    read snapshot (module docstring); 404 `not_found` for no such version, nothing written."""
    active = (await conn.execute(_ACTIVE)).one()
    await _found(conn, kind, version_id)
    checks = await _checks(conn, kind=kind, version_id=version_id, active=active, now=now)
    run = await gate_run(conn, checks.candidate)
    estimate = checks.estimate
    return ActivationPreviewOut(
        version_id=version_id,
        active=getattr(active, _SHIPPED[kind].pointer) == version_id,
        gate_state=checks.gate_state,
        latest_golden_run=None
        if run is None
        else GoldenRunOut(
            eval_run_id=run.eval_run_id,
            passed=run.passed,
            spearman_rho=run.spearman_rho,
            finished_at=run.finished_at,
        ),
        schema_verified=checks.schema_verified,
        unverified_models=sorted({model for _, model in checks.unverified}),
        targets=0 if estimate is None else estimate.targets,
        est_usd_online=_NO_USD if estimate is None else estimate.est_usd_online,
        est_usd_batch=_NO_USD if estimate is None else estimate.est_usd_batch,
        bulk_budget_usd=_NO_USD if estimate is None else estimate.bulk_budget_usd,
    )
