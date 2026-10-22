"""The immutable rows a settings version points at (spec §13.2, §11.7.4; plan Task 15, Deviations 14 and 16).

`derived(settings, profile=…)` gives the four payloads, `ensure_derived(conn, d)` their row ids. Each kind is
deduplicated by the content hash of its complete payload (`db.seed.content_hash`, the canonical JSON as
stored): an identical payload finds its row, a changed field creates one new row, and reverting finds the
earlier one. The payloads are the settings document's own objects, as `db.seed` derives those of version 1:
- the scope is `seed.scope_payload` (the group Scope without `max_ad_age_days`, as M0 stores it). With a
  profile, a `profile_relative` seniority range is resolved into the explicit range around the profile's
  level (`derived_seniority`), `below` and `above` kept, so one setting derives one scope per profile
  (Deviation 14). Without a profile it is kept as is, and the M1 filter passes it (M1 Deviation 8);
- the model policy is `scoring.model_policy`, the execution config `scoring.execution`, the evaluation policy
  `evaluation`.

A new evaluation policy takes `version_no = max + 1` and `projected_critic_rate =
DEFAULT_PROJECTED_CRITIC_RATE`, the §21.2 central 0.450: §11.7.4's replay needs the round-0 drafts of M3
(`seed.ensure_evaluation_policy`, Deviation 16).

`active_profile(conn, profile_version_id)` is the profile a scope is resolved with: the settings save and
retry-apply (Task 18) pass the active one.
"""

from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.seed import ensure_evaluation_policy, ensure_hashed, scope_payload
from jobhunter.db.sql import q
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.normalize.titles import SENIORITY_LADDER
from jobhunter.settings.model import Settings
from jobhunter.types import JSONValue, get_object

_PROFILE: Final = q("SELECT profile_json FROM profile_versions WHERE id = :id")


@dataclass(frozen=True)
class DerivedPayloads:
    scope: dict[str, JSONValue]
    model_policy: dict[str, JSONValue]
    execution: dict[str, JSONValue]
    evaluation: dict[str, JSONValue]


@dataclass(frozen=True)
class DerivedIds:
    scope_version_id: int
    model_policy_id: int
    execution_config_id: int
    evaluation_policy_id: int


def derived_seniority(profile_seniority: str, *, below: int, above: int) -> tuple[str, str]:
    """The explicit `(min, max)` around `profile_seniority` on `SENIORITY_LADDER`: the profile's level minus
    `below` and plus `above`, clamped to the ladder's ends."""
    if below < 0 or above < 0:
        raise ValueError(f"below and above must not be negative, got {below} and {above}")
    if profile_seniority not in SENIORITY_LADDER:
        raise ValueError(f"not a seniority level of the ladder: {profile_seniority!r}")
    level = SENIORITY_LADDER.index(profile_seniority)
    top = len(SENIORITY_LADDER) - 1
    return SENIORITY_LADDER[max(0, level - below)], SENIORITY_LADDER[min(top, level + above)]


def derived(settings: Settings, *, profile: ConfirmedProfile | None = None) -> DerivedPayloads:
    """The four derived payloads of `settings`, the scope resolved with `profile` (module docstring)."""
    payload = settings.to_payload()
    scope = scope_payload(payload)
    seniority = settings.scope.seniority_range
    if profile is not None and seniority.mode == "profile_relative":
        low, high = derived_seniority(profile.seniority, below=seniority.below, above=seniority.above)
        scope["seniority_range"] = {
            "mode": "explicit",
            "below": seniority.below,
            "above": seniority.above,
            "min": low,
            "max": high,
        }
    return DerivedPayloads(
        scope=scope,
        model_policy=get_object(payload, "scoring.model_policy"),
        execution=get_object(payload, "scoring.execution"),
        evaluation=get_object(payload, "evaluation"),
    )


async def ensure_derived(conn: AsyncConnection, d: DerivedPayloads) -> DerivedIds:
    """The ids of the rows of `d`, each found by its content hash or inserted, in the caller's transaction.

    A settings save calls it holding `active_config` `FOR UPDATE` (§13.3), which serialises the evaluation
    policies' `version_no` (`seed.ensure_evaluation_policy`).
    """
    return DerivedIds(
        scope_version_id=await ensure_hashed(conn, "scope_versions", d.scope),
        model_policy_id=await ensure_hashed(conn, "model_policies", d.model_policy),
        execution_config_id=await ensure_hashed(conn, "execution_configs", d.execution),
        evaluation_policy_id=await ensure_evaluation_policy(conn, d.evaluation),
    )


async def active_profile(conn: AsyncConnection, profile_version_id: object) -> ConfirmedProfile | None:
    """The profile of `profile_version_id` (`active_config.profile_version_id`), or `None` without one; a
    purged version has no content to resolve with."""
    if not isinstance(profile_version_id, int):
        return None
    profile_json = (await conn.execute(_PROFILE, {"id": profile_version_id})).scalar_one()
    return None if profile_json is None else ConfirmedProfile.model_validate(profile_json)
