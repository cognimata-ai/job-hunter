"""Profile confirm and activation (spec §12.6 step 2, §9.1; plan Task 21, Deviations 2, 10, 14, 19, 36 and
40, rulings R21-1 to R21-5).

`confirm_draft(conn, draft_id, *, expected_rescore_ack, now, defer)` confirms a `ready` draft and activates
its profile in the caller's **one transaction**, and returns the response `(status, body)`. The route takes
`active_config` `FOR UPDATE` first and runs it inside `run_idempotent` (`api.routes.profile`). In order:

1. `active_config` `FOR UPDATE` (a no-op under the route's lock), then the draft `FOR UPDATE`; no such draft
   is 404 `not_found`.
2. A draft already `confirmed` returns `(201, confirm_response)`: the body its confirm stored, whatever key
   the request carries and whichever profile is active now (Deviation 2). Nothing is written, and the ranking
   generation stays (R21-4).
3. Any state but `ready`, or a `ready` draft whose content was purged (§20.1), is 409 `conflict_state`; so is
   a stored profile that no longer validates as a `ConfirmedProfile` (step 4).
4. The profile is validated again: `draft_json` as a `ConfirmedProfile`, `validate_profile` against the
   approved text (against nothing for a native PDF) and `validate_user_entered`. Each issue whose code is in
   `BLOCKING_ISSUES` (Deviation 19) is a `FieldError` at its path, with its code and message; the other issues
   are warnings the review showed. Every string of `user_entered` that holds a contact
   (`contacts.scan_value`) is a `FieldError` at its path with the code `contact_found` (Deviation 36):
   `validate_user_entered` neither scans nor repairs the user's own values, and
   `profile_versions.profile_json` stays contact-stripped. Any of them is 422 `validation_failed`. The profile
   stored is the one `validate_profile` returns (repaired).
5. The re-score estimate (`estimate.rescore_estimate`: plain reads, the month's `counted_usd` without a lock).
   With retained revisions (`targets > 0`) and no `expected_rescore_ack`, 422 `validation_failed` on
   `body.expected_rescore_ack` (`ack_required`). Every refusal comes before the first write.
6. (a) The version. `profile_hash` is `content_hash(profile_json)` (the canonical JSON). A **usable** version
   with that hash (`purged_at IS NULL`) is reused, its row taken `FOR KEY SHARE`: a profile purge, which
   locks the row `FOR UPDATE` before it deletes anything (§20.2), then waits for this commit, and one that got
   there first has made `purged_at IS NULL` false, so the read finds nothing and a new version is made. Under
   an active fence of kind `profile` on that version, 409 `profile_purge_in_progress`; the confirm can be
   repeated when the purge has finished. Otherwise a new row: `version_no = max + 1` (the `active_config` lock
   serialises the writers), the draft, its CV file, the profile, its hash and `confirmed_at = now`. A purged
   version is never reused or revived: identical content gets a new id, which §7.4's partial unique index
   allows. Then the fault point `confirm.after_version`.
7. (b) The tuple: `save.ensure_tuple` with the version and the active model policy (it reads the active rubric
   and prompt-schema versions itself).
8. (c) `active_config.profile_version_id` and `llm_tuple_id` set, then the fault point `confirm.after_flip`.
   (d) to (f) are M3's (Deviation 10).
9. (g) The scope: `derive.derived(active settings, profile=new)` gives the desired scope (`ensure_derived`):
   with `seniority_range.mode = explicit` it is the settings' own scope, with `profile_relative` the explicit
   range around the profile's level (Deviation 14). `scope_apply.set_desired_scope` reconciles the pending
   pointer on **every** confirm: confirming A, then B (B's apply pending), then A again clears the obsolete
   pending B, whose apply then ends obsolete instead of flipping the filters to B's seniority range. When it
   returns `None` (the desired scope is the active one), nothing is left unapplied, and every open
   `scope.apply_failed` is resolved in step 12, at the save's position.
10. (h) `vectors.request_embedding` (its dedup key reuses a live operation, and its handler does nothing for
    complete vectors), then `proposals.request_proposal`, unless the version already has a `ready` proposal
    (R21-5: an identical re-confirm pays for no new call; `request_proposal` itself reuses a running one). The
    operations and their jobs (`defer`) commit or roll back with the activation (C24).
    `create_targets_for_tuple` is M3's.
11. (i) The draft `ready → confirmed`, with `confirmed_profile_version_id` and `confirm_response := body`.
12. `scope_apply.resolve_apply_failed` when step 9 asked for it, then `ranking.bump` **last** (R21-1:
    `ranking_state` is the last lock of §9.1), on every confirm that reaches (i) (R21-4). Then the fault
    point `confirm.before_commit`.

The body (`ConfirmOutcome`, §14.2's 201) holds ids and decimal strings only: the version, `targets_to_create`
(the estimate's count, the one the user acknowledged and M3's `create_targets_for_tuple` creates; R21-3),
`est_usd` (`est_usd_online`, the quantity §6.8 calls `est_usd`; R21-2) and `bulk_budget_usd`.

**Lock order** (§9.1: `active_config` → owner row → `llm_budget` → … → other domain rows →
`ranking_state`): `active_config` first; no owner row is locked and the month row is only read; then the
other domain rows: the draft, a reused version (`FOR KEY SHARE`, then `FOR NO KEY UPDATE` in
`request_proposal`, which a key share never blocks), and the rows this transaction inserts (version, tuple,
derived scope, operations, proposal), then the alerts and `ranking_state`. The embedding is requested before
the proposal: a running `embed_profile` write holds the version `FOR SHARE`, which a key share lets through
and `FOR NO KEY UPDATE` waits for. A live operation of a dedup key is reused, never locked.

The module does not log: the route logs the ids that `activated` collects, after its commit. `afault_point`
is a name of this module, so a test replaces it.
"""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.errors import FieldError, Problem
from jobhunter.db import ranking
from jobhunter.db.seed import content_hash
from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition
from jobhunter.errors import InvariantViolation
from jobhunter.faults import afault_point
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.profile import estimate, vectors
from jobhunter.profile.contacts import scan_value
from jobhunter.profile.validate import validate_profile, validate_user_entered
from jobhunter.queries import proposals
from jobhunter.settings import save, scope_apply
from jobhunter.settings.derive import derived, ensure_derived
from jobhunter.settings.dryrun import active_settings
from jobhunter.types import JSONValue
from jobhunter.worker.tasks import Deferrer

# Deviation 19: the issues that make the stored profile wrong. The others (`ungrounded`, `unverifiable`,
# `years_mismatch`, `language_without_evidence`, `contact_removed`) are warnings the user may accept.
BLOCKING_ISSUES: Final = frozenset({"limit", "date_invalid", "date_order", "code_other"})
CONTACT_FOUND: Final = "contact_found"
ACK_FIELD: Final = "body.expected_rescore_ack"
ACK_REQUIRED: Final = "ack_required"
AFTER_VERSION_POINT: Final = "confirm.after_version"
AFTER_FLIP_POINT: Final = "confirm.after_flip"
BEFORE_COMMIT_POINT: Final = "confirm.before_commit"
_DRAFTS: Final = "profile_drafts"
_CREATED: Final = 201

_LOCK_ACTIVE: Final = q("SELECT model_policy_id FROM active_config WHERE id = 1 FOR UPDATE")
_LOCK_DRAFT: Final = q(
    "SELECT status, cv_file_id, native_pdf, reviewed_text, draft_json, confirm_response "
    "FROM profile_drafts WHERE id = :d FOR UPDATE"
)
_USABLE_VERSION: Final = q(
    "SELECT id FROM profile_versions WHERE profile_hash = :h AND purged_at IS NULL FOR KEY SHARE"
)
_FENCED: Final = q(
    "SELECT EXISTS (SELECT 1 FROM purge_fences WHERE released_at IS NULL AND kind = 'profile' "
    "AND profile_version_id = :v)"
)
_INSERT_VERSION: Final = q(
    "INSERT INTO profile_versions (version_no, draft_id, cv_file_id, profile_json, profile_hash, "
    "confirmed_at) SELECT coalesce(max(version_no), 0) + 1, :d, :cv, :profile, :h, :now "
    "FROM profile_versions RETURNING id",
    jsonb=["profile"],
)
_ACTIVATE: Final = q(
    "UPDATE active_config SET profile_version_id = :v, llm_tuple_id = :t, updated_at = :now WHERE id = 1"
)
_READY_PROPOSAL: Final = q(
    "SELECT EXISTS (SELECT 1 FROM query_proposals WHERE profile_version_id = :v AND status = 'ready')"
)


@dataclass(frozen=True)
class ConfirmOutcome:
    """§14.2's 201 body of a confirm (module docstring); `body()` gives it with the decimals as strings."""

    profile_version_id: int
    targets_to_create: int
    est_usd: Decimal
    bulk_budget_usd: Decimal

    def body(self) -> dict[str, JSONValue]:
        return {
            "profile_version_id": self.profile_version_id,
            "targets_to_create": self.targets_to_create,
            "est_usd": format(self.est_usd, "f"),
            "bulk_budget_usd": format(self.bulk_budget_usd, "f"),
        }


@dataclass(frozen=True)
class Activated:
    """The ids of one activation, for the caller's log line after its commit."""

    draft_id: int
    version_id: int
    version_created: bool
    llm_tuple_id: int
    apply_scope_operation_id: int | None
    embed_operation_id: int
    proposal_id: int | None
    targets_to_create: int


def draft_not_found() -> Problem:
    """404 `not_found` for a draft id with no row (the confirm and the estimate route)."""
    return Problem(404, "not_found", "Profile draft not found")


def _not_ready(status: str) -> Problem:
    return Problem(
        409,
        "conflict_state",
        "The draft is not in a state that allows this",
        detail=f"The draft is {status}; this needs it ready.",
    )


def _content_purged() -> Problem:
    return Problem(
        409,
        "conflict_state",
        "The draft's content was deleted",
        detail="The extracted profile of this draft was deleted after 30 days; upload or paste the CV again.",
    )


def _schema_mismatch() -> Problem:
    return Problem(
        409,
        "conflict_state",
        "The draft's profile cannot be read",
        detail="The draft's profile no longer matches the profile schema; discard it and upload or paste the "
        "CV again.",
    )


def _validation_failed(errors: list[FieldError]) -> Problem:
    return Problem(422, "validation_failed", "Profile validation failed", errors=errors)


def _purge_in_progress() -> Problem:
    return Problem(
        409,
        "profile_purge_in_progress",
        "This profile is being deleted",
        detail="An identical profile version is being purged; confirm again when the purge has finished.",
    )


def _strings(value: JSONValue, path: str) -> Iterator[tuple[str, str]]:
    """Every string in `value` with its issue path (`.` between names, `[i]` after a list)."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")


def user_entered_contacts(profile: ConfirmedProfile) -> list[FieldError]:
    """A `contact_found` error for each string of `user_entered` that holds a contact (Deviation 36). The
    message names the categories, never the value."""
    errors: list[FieldError] = []
    for path, value in _strings(profile.user_entered.model_dump(mode="json"), "user_entered"):
        found = scan_value(value)
        if found:
            message = f"Contact data ({', '.join(found)}) is not allowed in this value; remove it to confirm."
            errors.append(FieldError(field=path, code=CONTACT_FOUND, message=message))
    return errors


def revalidated(
    draft_json: JSONValue, *, transmitted_text: str | None, today: date
) -> tuple[ConfirmedProfile, list[FieldError]]:
    """Step 4: the repaired profile of `draft_json` and the errors that refuse its confirm (pure). A stored
    profile that is no `ConfirmedProfile` is 409 `conflict_state`; the validation error, which quotes the
    stored values, goes nowhere."""
    try:
        draft: ConfirmedProfile | None = ConfirmedProfile.model_validate(draft_json)
    except ValidationError:
        draft = None  # raised below, outside the handler: the problem carries no `__context__` either
    if draft is None:
        raise _schema_mismatch()
    profile, issues = validate_profile(draft, transmitted_text=transmitted_text, today=today)
    issues = [*issues, *validate_user_entered(profile.user_entered)]
    errors = [
        FieldError(field=issue.path, code=issue.code, message=issue.message)
        for issue in issues
        if issue.code in BLOCKING_ISSUES
    ]
    return profile, [*errors, *user_entered_contacts(profile)]


async def _version(
    conn: AsyncConnection, draft_id: int, cv_file_id: int, profile_json: JSONValue, *, now: datetime
) -> tuple[int, bool]:
    """Step 6: `(version_id, created)`."""
    digest = content_hash(profile_json)
    usable = (await conn.execute(_USABLE_VERSION, {"h": digest})).scalar_one_or_none()
    if usable is not None:
        if (await conn.execute(_FENCED, {"v": usable})).scalar_one():
            raise _purge_in_progress()
        return int(usable), False
    params = {"d": draft_id, "cv": cv_file_id, "profile": profile_json, "h": digest, "now": now}
    return int((await conn.execute(_INSERT_VERSION, params)).scalar_one()), True


async def confirm_draft(
    conn: AsyncConnection,
    draft_id: int,
    *,
    expected_rescore_ack: bool,
    now: datetime,
    defer: Deferrer,
    activated: list[Activated] | None = None,
) -> tuple[int, dict[str, JSONValue]]:
    """Confirm draft `draft_id` and activate its profile in the caller's transaction (module docstring); the
    response `(status, body)`. An activation's ids are appended to `activated` when it is given."""
    model_policy_id = (await conn.execute(_LOCK_ACTIVE)).scalar_one()
    draft = (await conn.execute(_LOCK_DRAFT, {"d": draft_id})).first()
    if draft is None:
        raise draft_not_found()
    if draft.status == "confirmed":
        if not isinstance(draft.confirm_response, dict):
            raise InvariantViolation(f"profile draft {draft_id} is confirmed without its stored response")
        return _CREATED, draft.confirm_response
    if draft.status != "ready":
        raise _not_ready(str(draft.status))
    if draft.draft_json is None:
        raise _content_purged()

    transmitted = None if draft.native_pdf else draft.reviewed_text
    # the contact patterns over every value: off the event loop
    profile, errors = await asyncio.to_thread(
        revalidated, draft.draft_json, transmitted_text=transmitted, today=now.date()
    )
    if errors:
        raise _validation_failed(errors)
    settings = await active_settings(conn)
    preview = await estimate.rescore_estimate(conn, settings, now=now)
    if preview.targets > 0 and not expected_rescore_ack:
        message = f"Confirming re-scores {preview.targets} retained ads; acknowledge the estimate to confirm."
        raise _validation_failed([FieldError(field=ACK_FIELD, code=ACK_REQUIRED, message=message)])

    profile_json = profile.model_dump(mode="json")
    version_id, version_created = await _version(conn, draft_id, int(draft.cv_file_id), profile_json, now=now)
    await afault_point(AFTER_VERSION_POINT)
    tuple_id = await save.ensure_tuple(
        conn, profile_version_id=version_id, model_policy_id=int(model_policy_id)
    )
    await conn.execute(_ACTIVATE, {"v": version_id, "t": tuple_id, "now": now})
    await afault_point(AFTER_FLIP_POINT)

    ids = await ensure_derived(conn, derived(settings, profile=profile))
    apply_id = await scope_apply.set_desired_scope(conn, ids.scope_version_id, now=now, defer=defer)
    embed_id, _ = await vectors.request_embedding(conn, version_id, defer=defer)
    proposal_id: int | None = None
    # A benign race: a proposal of this version published between this read and `request_proposal` (its
    # publish does not wait for this transaction) costs one more proposal, never a wrong one.
    if not (await conn.execute(_READY_PROPOSAL, {"v": version_id})).scalar_one():
        proposal_id, _, _ = await proposals.request_proposal(conn, version_id, defer=defer)

    outcome = ConfirmOutcome(
        profile_version_id=version_id,
        targets_to_create=preview.targets,
        est_usd=preview.est_usd_online,
        bulk_budget_usd=preview.bulk_budget_usd,
    )
    body = outcome.body()
    await transition(
        conn,
        _DRAFTS,
        draft_id,
        ["ready"],
        "confirmed",
        reason=CLEAR_REASON,
        set_={"confirmed_profile_version_id": version_id, "confirm_response": body, "updated_at": now},
    )
    if apply_id is None:  # other domain rows: after the draft, before `ranking_state` (module docstring)
        await scope_apply.resolve_apply_failed(conn)
    await ranking.bump(conn)  # the last lock (§9.1, R21-1)
    await afault_point(BEFORE_COMMIT_POINT)
    if activated is not None:
        activated.append(
            Activated(
                draft_id=draft_id,
                version_id=version_id,
                version_created=version_created,
                llm_tuple_id=tuple_id,
                apply_scope_operation_id=apply_id,
                embed_operation_id=embed_id,
                proposal_id=proposal_id,
                targets_to_create=preview.targets,
            )
        )
    return _CREATED, body
