# tests/integration/test_run_settings.py
import json

import pytest
import sqlalchemy as sa
from jobhunter.db.seed import load_settings_v1, scope_payload
from jobhunter.run_settings import (
    ScopeLocation,
    SeniorityRange,
    load_run_settings,
    scope_from_payload,
    slug,
)

pytestmark = pytest.mark.integration


async def test_seeded_defaults(engine) -> None:
    async with engine.connect() as c:
        sid = (await c.execute(sa.text("SELECT settings_version_id FROM active_config"))).scalar_one()
        s = await load_run_settings(c, sid)
        scope_version_id = (
            await c.execute(sa.text("SELECT scope_version_id FROM active_config"))
        ).scalar_one()
    assert (s.settings_version_id, s.scope_version_id) == (sid, scope_version_id)
    assert (
        s.scope.locations,
        s.scope.remote_within_germany,
        s.scope.max_ad_age_days,
        s.stale_ttl_days,
        s.interval_s,
    ) == (
        (),
        True,
        30,
        30,
        21600,
    )
    assert (
        s.quota.jsearch_monthly_limit,
        s.quota.serpapi_monthly_limit,
        s.quota.reserve_pct,
        s.quota.serpapi_hourly_cap,
        s.quota.paid_max_requests_per_run,
    ) == (200, 250, 10, 45, "auto")
    assert (
        s.quota.adzuna_cap_minute,
        s.quota.adzuna_cap_day,
        s.quota.adzuna_cap_week,
        s.quota.adzuna_cap_month,
    ) == (
        20,
        225,
        900,
        2250,
    )
    assert s.sources.jsearch and s.sources.ats and s.queries == {} and s.watchlist == {}
    assert s.schedule_include_paid is True
    assert s.scope.employment_types == frozenset(
        {
            "full_time",
            "part_time",
            "contract",
            "temporary",
            "internship",
            "working_student",
            "apprenticeship",
            "freelance",
        }
    )
    assert s.scope.work_modes == frozenset({"remote", "hybrid", "onsite"})
    assert s.scope.seniority_range == SeniorityRange("profile_relative", 1, 1, None, None)
    assert (
        s.scope.accepted_ad_languages == frozenset({"de", "en"}) and s.scope.excluded_companies == frozenset()
    )


SETTINGS_V2 = sa.text(
    "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, effect_classes) "
    "SELECT 2, schema_version, CAST(:payload AS jsonb), scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, '{collection}' FROM settings_versions WHERE version_no = 1 "
    "RETURNING id"
)


async def test_a_written_version_with_lists_locations_and_an_explicit_ttl(engine) -> None:
    settings = load_settings_v1()
    settings["scope"] = {
        **scope_payload(settings),
        "max_ad_age_days": 21,
        "locations": [
            {
                "label": "Frankfurt am Main",
                "gazetteer_id": "geonames:2925533",
                "lat": 50.11,
                "lon": 8.68,
                "radius_km": 30,
            },
            {"label": "Köln", "gazetteer_id": "geonames:2886242", "lat": 50.94, "lon": 6.96, "radius_km": 5},
        ],
        "excluded_companies": ["acme", "initech"],
        "excluded_keywords": ["Werkstudent", "Praktikum"],
        "industries_exclude": ["Gambling"],
        "seniority_range": {"mode": "explicit", "below": 0, "above": 2, "min": "junior", "max": "senior"},
    }
    settings["sources"] = {
        "enabled": {"jsearch": True, "serpapi": False, "adzuna": True, "arbeitnow": False, "ats": True}
    }
    settings["queries"] = [{"query_id": 4, "enabled": True}, {"query_id": 9, "enabled": False}]
    settings["watchlist"] = [{"watchlist_company_id": 2, "enabled": True}]
    settings["collection"] = {"stale_ttl_days": 14}
    settings["schedule"] = {**settings["schedule"], "include_paid": False}  # type: ignore[dict-item]
    settings["quota"] = {**settings["quota"], "paid_max_requests_per_run": 12}  # type: ignore[dict-item]
    async with engine.begin() as c:
        version_id = (await c.execute(SETTINGS_V2, {"payload": json.dumps(settings)})).scalar_one()
        s = await load_run_settings(c, version_id)
    assert [(loc.label, loc.key, loc.radius_km) for loc in s.scope.locations] == [
        ("Frankfurt am Main", "frankfurt-am-main", 30),
        ("Köln", "koln", 5),
    ]
    assert s.scope.locations[0] == ScopeLocation(
        "Frankfurt am Main", "geonames:2925533", 50.11, 8.68, 30, "frankfurt-am-main"
    )
    assert (s.scope.max_ad_age_days, s.stale_ttl_days, s.schedule_include_paid) == (21, 14, False)
    assert s.scope.excluded_companies == frozenset({"acme", "initech"})
    assert s.scope.excluded_keywords == ("Werkstudent", "Praktikum")
    assert s.scope.industries_exclude == frozenset({"gambling"})
    assert s.scope.seniority_range == SeniorityRange("explicit", 0, 2, "junior", "senior")
    assert (s.sources.serpapi, s.sources.arbeitnow, s.sources.adzuna) == (False, False, True)
    assert (s.queries, s.watchlist, s.quota.paid_max_requests_per_run) == ({4: True, 9: False}, {2: True}, 12)
    async with engine.connect() as c:
        with pytest.raises(LookupError):
            await load_run_settings(c, version_id + 1000)


def test_a_scope_version_payload_has_no_max_ad_age() -> None:
    scope = scope_from_payload(scope_payload(load_settings_v1()))
    assert scope.max_ad_age_days is None and scope.remote_within_germany is True


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("Berlin", "berlin"),
        ("Frankfurt (Oder)", "frankfurt-oder"),
        ("  Halle (Saale) ", "halle-saale"),
        ("München", "munchen"),
        ("Gießen", "giessen"),
        ("St. Ingbert", "st-ingbert"),
        ("Baden-Baden", "baden-baden"),
    ],
)
def test_location_keys_are_folded_slugs(label: str, key: str) -> None:
    assert slug(label) == key


def test_a_label_without_letters_or_digits_has_no_key() -> None:
    with pytest.raises(ValueError, match="slug"):
        slug(" (\u2013) ")  # an en dash: no letter, no digit
