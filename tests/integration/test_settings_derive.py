"""The derived immutable rows (spec §13.2, §11.7.4, §21.2; plan Task 15, Deviations 14 and 16).

`ensure_derived(conn, derived(settings, profile=…))` gives the ids of the scope version, model policy,
execution config and evaluation policy, deduplicated by the content hash of each payload: an identical
payload reuses every row (the seeded ones included), a changed field creates exactly one new row of its kind,
and reverting reuses the earlier row. A new evaluation policy takes `version_no = max + 1` and the §21.2
central `projected_critic_rate` of 0.450.
"""

import copy
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.db.seed import DEFAULT_PROJECTED_CRITIC_RATE, content_hash, load_settings_v1
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.settings.derive import DerivedIds, derived, ensure_derived
from jobhunter.settings.model import Settings
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

SEEDED = sa.text(
    "SELECT scope_version_id, model_policy_id, execution_config_id, evaluation_policy_id "
    "FROM settings_versions WHERE version_no = 1"
)
COUNTS = sa.text(
    "SELECT (SELECT count(*) FROM scope_versions), (SELECT count(*) FROM model_policies), "
    "(SELECT count(*) FROM execution_configs), (SELECT count(*) FROM evaluation_policies)"
)
POLICY = sa.text(
    "SELECT version_no, policy_hash, payload, projected_critic_rate FROM evaluation_policies WHERE id = :id"
)
SCOPE = sa.text("SELECT scope_hash, payload FROM scope_versions WHERE id = :id")


def settings_with(**groups: dict[str, Any]) -> Settings:
    """The seeded settings with the fields of each named group replaced."""
    doc = copy.deepcopy(load_settings_v1())
    for group, fields in groups.items():
        target = doc[group]
        assert isinstance(target, dict)
        target.update(fields)
    return Settings.model_validate(doc)


def profile(seniority: str) -> ConfirmedProfile:
    """A confirmed profile of an invented candidate: only its seniority matters here."""
    return ConfirmedProfile.model_validate(
        {
            "summary": "Datenanalystin mit Erfahrung in Logistikprojekten.",
            "target_titles": [{"title": "Data Analyst", "lang": "en"}],
            "seniority": seniority,
            "total_years_experience": 3.0,
            "roles": [],
            "skills": [],
            "languages": [],
            "education": [],
            "certifications": [],
            "domains": [],
        }
    )


async def seeded_ids(conn: AsyncConnection) -> DerivedIds:
    row = (await conn.execute(SEEDED)).one()
    return DerivedIds(*(int(value) for value in row))


async def counts(conn: AsyncConnection) -> tuple[int, ...]:
    return tuple(int(value) for value in (await conn.execute(COUNTS)).one())


async def test_an_identical_payload_reuses_every_row(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        seeded = await seeded_ids(conn)
        before = await counts(conn)
        first = await ensure_derived(conn, derived(Settings.model_validate(load_settings_v1())))
        second = await ensure_derived(conn, derived(Settings()))
        assert first == second == seeded
        assert await counts(conn) == before


async def test_a_changed_critic_threshold_creates_exactly_one_evaluation_policy(engine: AsyncEngine) -> None:
    changed = settings_with(evaluation={"relevance_threshold": 55, "t2_band": 8})
    async with engine.begin() as conn:
        seeded = await seeded_ids(conn)
        scopes, models, executions, policies = await counts(conn)
        top = int(
            (await conn.execute(sa.text("SELECT max(version_no) FROM evaluation_policies"))).scalar_one()
        )
        d = derived(changed)
        new = await ensure_derived(conn, d)
        assert (new.scope_version_id, new.model_policy_id, new.execution_config_id) == (
            seeded.scope_version_id,
            seeded.model_policy_id,
            seeded.execution_config_id,
        )
        assert new.evaluation_policy_id != seeded.evaluation_policy_id
        assert await counts(conn) == (scopes, models, executions, policies + 1)
        row = (await conn.execute(POLICY, {"id": new.evaluation_policy_id})).one()
        assert (row.version_no, bytes(row.policy_hash), row.payload, row.projected_critic_rate) == (
            top + 1,
            content_hash(d.evaluation),
            d.evaluation,
            Decimal("0.450"),
        )
        assert DEFAULT_PROJECTED_CRITIC_RATE == Decimal("0.450")
        # the same change again finds its row; reverting reuses the seeded one
        assert await ensure_derived(conn, derived(changed)) == new
        assert await ensure_derived(conn, derived(Settings())) == seeded
        assert await counts(conn) == (scopes, models, executions, policies + 1)


async def test_each_kind_of_row_changes_alone(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        seeded = await seeded_ids(conn)
        before = await counts(conn)
        budget_only = settings_with(scoring={"batch_max_items": 400}, llm={"cap_usd": "80.00"})
        assert await ensure_derived(conn, derived(budget_only)) == seeded  # no derived row holds them
        doc: dict[str, Any] = copy.deepcopy(load_settings_v1())
        doc["scoring"]["execution"]["critic"]["deadline_s"] = 900
        doc["scoring"]["model_policy"]["nodes"]["critic"]["effort"] = "high"
        doc["scope"]["excluded_keywords"] = ["Praktikum"]
        doc["scope"]["max_ad_age_days"] = 21  # admission, not scope: no new scope row by itself
        new = await ensure_derived(conn, derived(Settings.model_validate(doc)))
        assert new.evaluation_policy_id == seeded.evaluation_policy_id
        assert (
            len(
                {new.scope_version_id, new.model_policy_id, new.execution_config_id}
                & {
                    seeded.scope_version_id,
                    seeded.model_policy_id,
                    seeded.execution_config_id,
                }
            )
            == 0
        )
        assert await counts(conn) == (before[0] + 1, before[1] + 1, before[2] + 1, before[3])
        only_age = settings_with(scope={"max_ad_age_days": 21})
        assert (await ensure_derived(conn, derived(only_age))).scope_version_id == seeded.scope_version_id


async def test_one_profile_relative_setting_derives_one_scope_per_profile(engine: AsyncEngine) -> None:
    settings = Settings()  # seniority_range profile_relative, ±1
    async with engine.begin() as conn:
        seeded = await seeded_ids(conn)
        before = await counts(conn)
        junior = await ensure_derived(conn, derived(settings, profile=profile("junior")))
        senior = await ensure_derived(conn, derived(settings, profile=profile("senior")))
        assert len({junior.scope_version_id, senior.scope_version_id, seeded.scope_version_id}) == 3
        assert (junior.model_policy_id, junior.execution_config_id, junior.evaluation_policy_id) == (
            seeded.model_policy_id,
            seeded.execution_config_id,
            seeded.evaluation_policy_id,
        )
        assert (senior.model_policy_id, senior.execution_config_id, senior.evaluation_policy_id) == (
            seeded.model_policy_id,
            seeded.execution_config_id,
            seeded.evaluation_policy_id,
        )
        ranges = {}
        for name, ids in (("junior", junior), ("senior", senior)):
            row = (await conn.execute(SCOPE, {"id": ids.scope_version_id})).one()
            assert bytes(row.scope_hash) == content_hash(row.payload)
            ranges[name] = row.payload["seniority_range"]
        assert ranges == {
            "junior": {"mode": "explicit", "below": 1, "above": 1, "min": "apprentice", "max": "mid"},
            "senior": {"mode": "explicit", "below": 1, "above": 1, "min": "mid", "max": "lead"},
        }
        assert await counts(conn) == (before[0] + 2, *before[1:])
        # the same profile again reuses its scope; without a profile the seeded profile_relative scope
        assert await ensure_derived(conn, derived(settings, profile=profile("junior"))) == junior
        assert await ensure_derived(conn, derived(settings)) == seeded
        assert await counts(conn) == (before[0] + 2, *before[1:])
