"""Settings validation (spec §13.1, §6.9.1, §10.3 G5, §11.13; plan Task 15): `validate_fields` answers
with `FieldIssue(field, code, message)`, paths such as `payload.scope.locations[0].radius_km` and the codes
`out_of_range`, `invalid`, `required`, `conflict` and `unsupported`.

One table covers every §13.1 range at its edges and one past them, the cross-field rules, model and effort
support, the execution ranges and the §6.9.1 overrun-buffer bound. The bound figures follow the overhead
V19 re-measured in M3 Task 4 (1,878 tokens, `docs/spikes/V19.md`, "M3 re-count"): $1.23 at the defaults (500
batch items) and $23.53 at 10,000 batch items (spec revision 3.4, Ruling R15-1, had $1.72 and $33.07 at
2,640); a test derives both from `bounds` and `pricing`, so a change of either constant fails loudly.
Warnings (`eval_uncalibrated`, `serpapi_disabled_no_fallback`) are `warnings_only`'s (Ruling R15-2).
"""

import copy
from collections.abc import Iterator, Mapping
from decimal import ROUND_CEILING, Decimal
from types import MappingProxyType
from typing import Any, Final

import pytest
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import bounds, pricing
from jobhunter.llm.pricing import Effort, Model
from jobhunter.quota.dryrun import Warning
from jobhunter.settings.model import Settings
from jobhunter.settings.validate import (
    FieldIssue,
    fake_llm_process,
    gate_transports,
    overrun_buffer_bound,
    validate_fields,
    warnings_only,
)

NODES = ("generate", "revise", "critic", "extract_profile", "propose_queries")
OPUS: Final[Model] = "claude-opus-5-5"
SONNET: Final[Model] = "claude-sonnet-5-5"
HAIKU: Final[Model] = "claude-haiku-5-5"
FABLE: Final[Model] = "claude-fable-5-1"
HAIKU_NODES = {node: {"model": HAIKU, "effort": "medium"} for node in NODES}


def apply(changes: Mapping[str, Any]) -> dict[str, Any]:
    """The seeded document with each dotted path of `changes` set to its value."""
    doc = copy.deepcopy(load_settings_v1())
    for path, value in changes.items():
        *parents, last = path.split(".")
        node: Any = doc
        for part in parents:
            node = node[part]
        node[last] = value
    return doc


def loc(i: int, **overrides: Any) -> dict[str, Any]:
    """A gazetteer place of an invented town."""
    return {
        "label": f"Musterstadt {i}",
        "gazetteer_id": f"geonames:{1000 + i}",
        "lat": 50.0,
        "lon": 8.0,
        "radius_km": 30,
        **overrides,
    }


def out(path: str) -> list[tuple[str, str]]:
    return [(f"payload.{path}", "out_of_range")]


def bad(path: str, code: str = "invalid") -> list[tuple[str, str]]:
    return [(f"payload.{path}", code)]


OK: list[tuple[str, str]] = []

# (path, lowest, highest, the other fields an edge needs to be valid)
INT_RANGES: list[tuple[str, int, int, dict[str, Any]]] = [
    ("schedule.interval_hours", 1, 168, {}),
    ("scope.seniority_range.below", 0, 3, {}),
    ("scope.seniority_range.above", 0, 3, {}),
    ("scope.min_salary_eur_year", 0, 500_000, {}),
    ("scope.max_ad_age_days", 1, 90, {}),
    ("evaluation.t2_band", 0, 20, {}),
    ("evaluation.t8_sample_pct", 0, 100, {}),
    ("collection.stale_ttl_days", 7, 90, {}),
    ("quota.jsearch_monthly_limit", 1, 100_000, {}),
    ("quota.serpapi_monthly_limit", 1, 100_000, {}),
    ("quota.reserve_pct", 0, 50, {}),
    ("quota.serpapi_hourly_cap", 1, 49, {}),
    ("quota.adzuna_cap_minute", 1, 25, {}),
    ("quota.adzuna_cap_day", 1, 250, {}),
    ("quota.adzuna_cap_week", 1, 1_000, {}),
    ("quota.adzuna_cap_month", 1, 2_500, {}),
    ("quota.paid_max_requests_per_run", 1, 100, {}),
    (
        "scoring.execution.critic.max_tokens",
        1_024,
        32_000,
        {"scoring.execution.critic.max_tokens_retry": 64_000},
    ),
    (
        "scoring.execution.critic.max_tokens_retry",
        1_024,
        64_000,
        {"scoring.execution.critic.max_tokens": 1_024},
    ),
    ("scoring.execution.critic.deadline_s", 120, 1_800, {}),
    ("scoring.execution.extract_profile.deadline_s", 120, 1_800, {}),
    ("llm.nonscoring_reserve_pct", 0, 20, {}),
    ("scoring.max_targets_per_cycle", 1, 500, {}),
    ("scoring.min_cycle_targets", 1, 100, {}),
    ("scoring.batch_min_targets", 200, 100_000, {}),
    ("scoring.batch_max_items", 200, 10_000, {"llm.overrun_buffer_usd": "23.53"}),
    ("retention.score_history_months", 1, 36, {}),
]
# (path, lowest, just below, highest, just above, the other fields an edge needs to be valid)
DECIMAL_RANGES: list[tuple[str, str, str, str, str, dict[str, Any]]] = [
    ("llm.cap_usd", "1.00", "0.99", "10000.00", "10000.01", {}),
    # $0.50 is valid only where the §6.9.1 bound is lower: Haiku everywhere and 200 batch items give $0.13
    (
        "llm.overrun_buffer_usd",
        "0.50",
        "0.49",
        "50.00",
        "50.01",
        {"scoring.model_policy.nodes": HAIKU_NODES, "scoring.batch_max_items": 200},
    ),
    ("llm.min_bucket_capacity_usd", "0.10", "0.09", "100.00", "100.01", {}),
    ("evaluation.t2_straddle_max_coverage", "0", "-0.01", "1", "1.01", {}),
    ("scoring.bulk_rescore_max_budget_share", "0.1", "0.09", "1.0", "1.01", {}),
]


def range_cases() -> Iterator[Any]:
    for path, low, high, context in INT_RANGES:
        yield pytest.param({**context, path: low}, OK, id=f"{path}={low}")
        yield pytest.param({**context, path: low - 1}, out(path), id=f"{path}={low - 1}")
        yield pytest.param({**context, path: high}, OK, id=f"{path}={high}")
        yield pytest.param({**context, path: high + 1}, out(path), id=f"{path}={high + 1}")
    for path, lowest, below, highest, above, context in DECIMAL_RANGES:
        yield pytest.param({**context, path: lowest}, OK, id=f"{path}={lowest}")
        yield pytest.param({**context, path: below}, out(path), id=f"{path}={below}")
        yield pytest.param({**context, path: highest}, OK, id=f"{path}={highest}")
        yield pytest.param({**context, path: above}, out(path), id=f"{path}={above}")


C = pytest.param
CASES: list[Any] = [
    *range_cases(),
    # τ = relevance_threshold (0 to 100) and t3_min_mid (50 to 100) with t3_min_mid > τ: an edge of one range
    # can be valid only with the other field moved
    C({"evaluation.relevance_threshold": 0}, OK, id="relevance_threshold=0"),
    C({"evaluation.relevance_threshold": -1}, out("evaluation.relevance_threshold"), id="relevance=-1"),
    C({"evaluation.relevance_threshold": 99, "evaluation.t3_min_mid": 100}, OK, id="relevance_threshold=99"),
    C({"evaluation.relevance_threshold": 100}, bad("evaluation.t3_min_mid", "conflict"), id="relevance=100"),
    C({"evaluation.relevance_threshold": 101}, out("evaluation.relevance_threshold"), id="relevance=101"),
    C({"evaluation.t3_min_mid": 50, "evaluation.relevance_threshold": 49}, OK, id="t3_min_mid=50"),
    C(
        {"evaluation.t3_min_mid": 49, "evaluation.relevance_threshold": 40},
        out("evaluation.t3_min_mid"),
        id="t3=49",
    ),
    C({"evaluation.t3_min_mid": 100}, OK, id="t3_min_mid=100"),
    C({"evaluation.t3_min_mid": 101}, out("evaluation.t3_min_mid"), id="t3_min_mid=101"),
    C({"evaluation.t3_min_mid": 60}, bad("evaluation.t3_min_mid", "conflict"), id="t3_min_mid=relevance"),
    C({"evaluation.t3_min_mid": 55}, bad("evaluation.t3_min_mid", "conflict"), id="t3_min_mid<relevance"),
    # scope.locations: 0 to 10 entries, radius_km 1 to 200, 0 rejected (distance bands divide by it)
    C({"scope.locations": [loc(0, radius_km=1)]}, OK, id="radius_km=1"),
    C({"scope.locations": [loc(0, radius_km=0)]}, out("scope.locations[0].radius_km"), id="radius_km=0"),
    C({"scope.locations": [loc(0, radius_km=200)]}, OK, id="radius_km=200"),
    C(
        {"scope.locations": [loc(0), loc(1, radius_km=201)]},
        out("scope.locations[1].radius_km"),
        id="radius=201",
    ),
    C({"scope.locations": [loc(i) for i in range(10)]}, OK, id="10 locations"),
    C({"scope.locations": [loc(i) for i in range(11)]}, out("scope.locations"), id="11 locations"),
    C({"scope.locations": [loc(0, lat=90.5)]}, out("scope.locations[0].lat"), id="lat=90.5"),
    C({"scope.locations": [loc(0, lon=-180.5)]}, out("scope.locations[0].lon"), id="lon=-180.5"),
    C({"scope.locations": [loc(0, lat="50.0")]}, bad("scope.locations[0].lat"), id="lat string"),
    C(
        {"scope.locations": [{k: v for k, v in loc(0).items() if k != "radius_km"}]},
        bad("scope.locations[0].radius_km", "required"),
        id="location without radius",
    ),
    C({"scope.locations": [loc(0, label=" (-) ")]}, bad("scope.locations[0].label"), id="label without key"),
    C(
        {"scope.locations": [loc(0), loc(1, gazetteer_id="geonames:1000")]},
        bad("scope.locations[1].gazetteer_id", "conflict"),
        id="the same place twice",
    ),
    C(
        {"scope.locations": [loc(0, label="Bad Musterstadt"), loc(1, label="Bad-Musterstadt")]},
        bad("scope.locations[1].label", "conflict"),
        id="two labels with one location key",
    ),
    # `remote-de` is the key of the remote-within-Germany cells (sources.base.REMOTE_DE), on or off
    C(
        {"scope.locations": [loc(0, label="Remote DE")]},
        bad("scope.locations[0].label", "conflict"),
        id="remote-de",
    ),
    C(
        {"scope.locations": [loc(0, label="remote-de")], "scope.remote_within_germany": False},
        bad("scope.locations[0].label", "conflict"),
        id="remote-de with remote off",
    ),
    # cross-field: at least one location, or remote_within_germany
    C(
        {"scope.remote_within_germany": False},
        bad("scope.locations", "conflict"),
        id="no location, no remote",
    ),
    C({"scope.remote_within_germany": False, "scope.locations": [loc(0)]}, OK, id="a location, no remote"),
    # cross-field: industries_include ∩ industries_exclude = ∅ (case-folded)
    C(
        {"scope.industries_include": ["Fintech"], "scope.industries_exclude": ["Gaming", "FINTECH"]},
        bad("scope.industries_exclude[1]", "conflict"),
        id="include and exclude overlap",
    ),
    C({"scope.industries_include": ["Fintech"], "scope.industries_exclude": ["Gaming"]}, OK, id="disjoint"),
    # industries are stored case-folded: a repeat once folded is a conflict
    C(
        {"scope.industries_include": ["FinTech", "Logistik", "fintech"]},
        bad("scope.industries_include[2]", "conflict"),
        id="industry twice once folded",
    ),
    C(
        {"scope.industries_exclude": ["Gaming", " GAMING "]},
        bad("scope.industries_exclude[1]", "conflict"),
        id="excluded industry twice once folded",
    ),
    C(
        {"scope.industries_include": ["Gaming"], "scope.industries_exclude": ["gaming", "GAMING"]},
        [
            ("payload.scope.industries_exclude[0]", "conflict"),
            ("payload.scope.industries_exclude[1]", "conflict"),
        ],
        id="one issue per excluded entry",
    ),
    C(
        {"scope.industries_include": ["Öffentlicher Dienst", "Offentlicher Dienst"]},
        bad("scope.industries_include[1]", "conflict"),
        id="industry twice without its umlaut",
    ),
    # cross-field: seniority_range.min ≤ max
    C(
        {
            "scope.seniority_range": {
                "mode": "explicit",
                "below": 1,
                "above": 1,
                "min": "senior",
                "max": "junior",
            }
        },
        bad("scope.seniority_range.min", "conflict"),
        id="seniority min > max",
    ),
    C(
        {"scope.seniority_range": {"mode": "explicit", "below": 1, "above": 1, "min": "mid", "max": "mid"}},
        OK,
        id="seniority min = max",
    ),
    C(
        {"scope.seniority_range": {"mode": "explicit", "below": 0, "above": 0, "min": None, "max": "lead"}},
        OK,
        id="seniority open below",
    ),
    C({"scope.seniority_range.mode": "fixed"}, bad("scope.seniority_range.mode"), id="seniority mode"),
    C({"scope.seniority_range.min": "boss"}, bad("scope.seniority_range.min"), id="seniority level"),
    # cross-field: every source disabled is an error; SerpApi alone off is a warning (warnings_only)
    C(
        {"sources.enabled": dict.fromkeys(("jsearch", "serpapi", "adzuna", "arbeitnow", "ats"), False)},
        bad("sources.enabled", "conflict"),
        id="every source disabled",
    ),
    C({"sources.enabled.serpapi": False}, OK, id="serpapi disabled"),
    C({"sources.enabled.ats": "yes"}, bad("sources.enabled.ats"), id="source switch string"),
    # lists of strings: industries ≤ 30 of 2 to 60 characters, companies ≤ 200, keywords ≤ 100 of 2 to 60
    C({"scope.industries_include": [f"Branche {i:02}" for i in range(30)]}, OK, id="30 industries"),
    C(
        {"scope.industries_include": [f"Branche {i:02}" for i in range(31)]},
        out("scope.industries_include"),
        id="31",
    ),
    C(
        {"scope.industries_exclude": [f"Branche {i:02}" for i in range(31)]},
        out("scope.industries_exclude"),
        id="31x",
    ),
    C({"scope.industries_include": ["ab", "x" * 60]}, OK, id="industry of 2 and 60 characters"),
    C({"scope.industries_include": ["a"]}, out("scope.industries_include[0]"), id="industry of 1 character"),
    C(
        {"scope.industries_exclude": ["ab", "x" * 61]},
        out("scope.industries_exclude[1]"),
        id="industry of 61",
    ),
    C({"scope.excluded_companies": [f"firma-{i}" for i in range(200)]}, OK, id="200 companies"),
    C(
        {"scope.excluded_companies": [f"firma-{i}" for i in range(201)]},
        out("scope.excluded_companies"),
        id="201",
    ),
    C({"scope.excluded_companies": [""]}, out("scope.excluded_companies[0]"), id="empty company key"),
    # companies are stored as company keys (§8.3): a name without a key is invalid, a repeat once keyed is a
    # conflict
    C({"scope.excluded_companies": ["Zalando SE", "Vitaservices GmbH & Co. KG"]}, OK, id="company names"),
    C({"scope.excluded_companies": ["   "]}, bad("scope.excluded_companies[0]"), id="company name of spaces"),
    C(
        {"scope.excluded_companies": ["Zalando SE", "Acme", "zalando"]},
        bad("scope.excluded_companies[2]", "conflict"),
        id="company twice once keyed",
    ),
    C(
        {"scope.excluded_companies": ["Muster GmbH", "Muster AG"]},
        bad("scope.excluded_companies[1]", "conflict"),
        id="one company in two legal forms",
    ),
    C({"scope.excluded_keywords": [f"wort{i}" for i in range(100)]}, OK, id="100 keywords"),
    C(
        {"scope.excluded_keywords": [f"wort{i}" for i in range(101)]},
        out("scope.excluded_keywords"),
        id="101",
    ),
    C({"scope.excluded_keywords": ["ab", "x" * 60]}, OK, id="keyword of 2 and 60 characters"),
    C({"scope.excluded_keywords": ["a"]}, out("scope.excluded_keywords[0]"), id="keyword of 1 character"),
    C({"scope.excluded_keywords": ["x" * 61]}, out("scope.excluded_keywords[0]"), id="keyword of 61"),
    # set-valued fields: non-empty subsets, no value twice
    C({"scope.work_modes": ["onsite"]}, OK, id="one work mode"),
    C({"scope.work_modes": []}, out("scope.work_modes"), id="no work mode"),
    C({"scope.work_modes": ["remote", "remote"]}, bad("scope.work_modes"), id="work mode twice"),
    C({"scope.work_modes": ["office"]}, bad("scope.work_modes[0]"), id="unknown work mode"),
    C({"scope.employment_types": ["freelance"]}, OK, id="one employment type"),
    C({"scope.employment_types": []}, out("scope.employment_types"), id="no employment type"),
    C({"scope.employment_types": ["permanent"]}, bad("scope.employment_types[0]"), id="unknown employment"),
    C({"scope.accepted_ad_languages": ["other"]}, OK, id="ad language other"),
    C({"scope.accepted_ad_languages": []}, out("scope.accepted_ad_languages"), id="no ad language"),
    C({"scope.accepted_ad_languages": ["fr"]}, bad("scope.accepted_ad_languages[0]"), id="ad language fr"),
    C({"evaluation.critic_light_triggers": []}, OK, id="no light trigger"),
    C({"evaluation.critic_light_triggers": ["T2", "T3", "T8"]}, OK, id="every light trigger"),
    C(
        {"evaluation.critic_light_triggers": ["T5"]},
        bad("evaluation.critic_light_triggers[0]"),
        id="T5 light",
    ),
    C(
        {"evaluation.critic_light_triggers": ["T2", "T2"]},
        bad("evaluation.critic_light_triggers"),
        id="T2 twice",
    ),
    # enumerations and maps
    C({"scope.language_policy_default": "soft"}, OK, id="language policy soft"),
    C({"scope.language_policy_default": "strict"}, bad("scope.language_policy_default"), id="policy strict"),
    C({"scope.language_policy": {"fr": "soft", "other": "hard"}}, OK, id="language policy map"),
    C({"scope.language_policy": {"xx": "soft"}}, bad("scope.language_policy.xx"), id="language policy key"),
    C(
        {"scope.language_policy": {"de": "strict"}},
        bad("scope.language_policy.de"),
        id="language policy value",
    ),
    C({"scope.knockout_severity.degree": "hard"}, OK, id="degree hard"),
    C(
        {"scope.knockout_severity.degree": "medium"},
        bad("scope.knockout_severity.degree"),
        id="severity value",
    ),
    C({"scope.min_salary_eur_year": None}, OK, id="no salary minimum"),
    C({"scope.min_salary_eur_year": "50000"}, bad("scope.min_salary_eur_year"), id="salary as string"),
    C(
        {"scope.exclude_keywords_in_description": "no"},
        bad("scope.exclude_keywords_in_description"),
        id="bool",
    ),
    C({"schedule.anchor_local": "00:00"}, OK, id="anchor 00:00"),
    C({"schedule.anchor_local": "23:59"}, OK, id="anchor 23:59"),
    C({"schedule.anchor_local": "24:00"}, bad("schedule.anchor_local"), id="anchor 24:00"),
    C({"schedule.anchor_local": "6:00"}, bad("schedule.anchor_local"), id="anchor 6:00"),
    C({"display.timezone": "America/New_York"}, OK, id="time zone New York"),
    C({"display.timezone": "Mars/Olympus"}, bad("display.timezone"), id="unknown time zone"),
    C({"display.timezone": "../../etc/passwd"}, bad("display.timezone"), id="time zone path"),
    C({"display.ui_language": "de"}, OK, id="ui de"),
    C({"display.ui_language": "fr"}, bad("display.ui_language"), id="ui fr"),
    C({"notifications.push_min_severity": "critical"}, OK, id="push critical"),
    C({"notifications.push_min_severity": "info"}, bad("notifications.push_min_severity"), id="push info"),
    C({"collection.stale_ttl_days": None}, OK, id="stale ttl follows max_ad_age_days"),
    C({"quota.paid_max_requests_per_run": "auto"}, OK, id="paid max auto"),
    C(
        {"quota.paid_max_requests_per_run": "many"},
        bad("quota.paid_max_requests_per_run"),
        id="paid max word",
    ),
    C({"quota.paid_max_requests_per_run": True}, bad("quota.paid_max_requests_per_run"), id="paid max bool"),
    # entries: queries 0 to 40, watchlist ≤ 100, ids unique
    C({"queries": [{"query_id": i, "enabled": True} for i in range(1, 41)]}, OK, id="40 queries"),
    C({"queries": [{"query_id": i, "enabled": True} for i in range(1, 42)]}, out("queries"), id="41 queries"),
    C(
        {"queries": [{"query_id": 4, "enabled": True}, {"query_id": 4, "enabled": False}]},
        bad("queries[1].query_id", "conflict"),
        id="query twice",
    ),
    C({"queries": [{"query_id": 0, "enabled": True}]}, out("queries[0].query_id"), id="query id 0"),
    C({"queries": [{"query_id": 2**63 - 1, "enabled": True}]}, OK, id="query id bigint max"),
    C({"queries": [{"query_id": 2**63, "enabled": True}]}, out("queries[0].query_id"), id="query id 2^63"),
    C(
        {"watchlist": [{"watchlist_company_id": 2**63, "enabled": True}]},
        out("watchlist[0].watchlist_company_id"),
        id="company id 2^63",
    ),
    C({"queries": [{"query_id": 4}]}, bad("queries[0].enabled", "required"), id="query without enabled"),
    C(
        {"watchlist": [{"watchlist_company_id": i, "enabled": True} for i in range(1, 101)]},
        OK,
        id="100 watched",
    ),
    C(
        {"watchlist": [{"watchlist_company_id": i, "enabled": True} for i in range(1, 102)]},
        out("watchlist"),
        id="101",
    ),
    C(
        {
            "watchlist": [
                {"watchlist_company_id": 2, "enabled": True},
                {"watchlist_company_id": 2, "enabled": True},
            ]
        },
        bad("watchlist[1].watchlist_company_id", "conflict"),
        id="company twice",
    ),
    # model policy: supported models and efforts (G5)
    C({"scoring.model_policy.preset": "custom"}, OK, id="preset custom"),
    C({"scoring.model_policy.preset": "D"}, bad("scoring.model_policy.preset"), id="preset D"),
    C({"scoring.model_policy.nodes": HAIKU_NODES}, OK, id="Haiku everywhere"),
    C(
        {"scoring.model_policy.nodes.critic": {"model": "claude-gpt-9", "effort": "medium"}},
        bad("scoring.model_policy.nodes.critic.model", "unsupported"),
        id="unsupported model",
    ),
    C(
        {"scoring.model_policy.nodes.propose_queries": {"model": OPUS, "effort": "ultra"}},
        bad("scoring.model_policy.nodes.propose_queries.effort", "unsupported"),
        id="unsupported effort",
    ),
    C(
        {"scoring.model_policy.critic_light_effort": "minimal"},
        bad("scoring.model_policy.critic_light_effort", "unsupported"),
        id="unsupported light effort",
    ),
    C(
        {"scoring.model_policy.nodes.critic": {"model": 7, "effort": "low"}},
        bad("scoring.model_policy.nodes.critic.model"),
        id="model 7",
    ),
    C(
        {"scoring.model_policy.nodes.critic": {"model": OPUS}},
        bad("scoring.model_policy.nodes.critic.effort", "required"),
        id="node without effort",
    ),
    C({"scoring.model_policy.nodes.generate": {"model": SONNET, "effort": "max"}}, OK, id="generator max"),
    # execution: max_tokens_retry ≥ max_tokens
    C(
        {"scoring.execution.critic": {"max_tokens": 6000, "max_tokens_retry": 5999, "deadline_s": 600}},
        bad("scoring.execution.critic.max_tokens_retry", "conflict"),
        id="retry below first",
    ),
    C(
        {"scoring.execution.generate": {"max_tokens": 6000, "max_tokens_retry": 6000, "deadline_s": 600}},
        OK,
        id="retry equal first",
    ),
    C(
        {"scoring.execution.revise": {"max_tokens": 6000, "max_tokens_retry": 12000}},
        bad("scoring.execution.revise.deadline_s", "required"),
        id="execution without deadline",
    ),
    # the §6.9.1 overrun-buffer bound: $1.23 at the defaults, $23.53 at 10,000 batch items
    C({"llm.overrun_buffer_usd": "1.23"}, OK, id="buffer at the default bound"),
    C({"llm.overrun_buffer_usd": "1.22"}, out("llm.overrun_buffer_usd"), id="buffer below the default bound"),
    C({"scoring.batch_max_items": 10_000}, out("llm.overrun_buffer_usd"), id="10000 items, $2.00 buffer"),
    C(
        {"scoring.batch_max_items": 10_000, "llm.overrun_buffer_usd": "23.52"},
        out("llm.overrun_buffer_usd"),
        id="10000 items, $23.52 buffer",
    ),
    C({"scoring.batch_max_items": 10_000, "llm.overrun_buffer_usd": "23.53"}, OK, id="10000 items, $23.53"),
    # decimals are strings with at most two places for USD
    C({"llm.cap_usd": "250"}, OK, id="cap without places"),
    C({"llm.cap_usd": "100.005"}, bad("llm.cap_usd"), id="cap with three places"),
    C({"llm.cap_usd": "1e2"}, bad("llm.cap_usd"), id="cap in exponent form"),
    C({"llm.cap_usd": "NaN"}, bad("llm.cap_usd"), id="cap NaN"),
    C({"llm.cap_usd": "-0"}, out("llm.cap_usd"), id="cap negative zero"),
    C({"evaluation.t2_straddle_max_coverage": "0.700"}, OK, id="coverage with trailing zeros"),
    C({"evaluation.t2_straddle_max_coverage": "-0"}, OK, id="coverage negative zero"),
    # the rates take at most 2 decimal places (Ruling R15-5)
    C({"evaluation.t2_straddle_max_coverage": "0.65"}, OK, id="coverage with 2 places"),
    C({"evaluation.t2_straddle_max_coverage": "0.650"}, OK, id="coverage with a zero third place"),
    C(
        {"evaluation.t2_straddle_max_coverage": "0.655"},
        bad("evaluation.t2_straddle_max_coverage"),
        id="coverage with 3 places",
    ),
    C({"scoring.bulk_rescore_max_budget_share": "0.45"}, OK, id="share with 2 places"),
    C(
        {"scoring.bulk_rescore_max_budget_share": "0.455"},
        bad("scoring.bulk_rescore_max_budget_share"),
        id="share with 3 places",
    ),
    C({"llm.overrun_buffer_usd": "2.000"}, OK, id="buffer with a zero third place"),
    C({"llm.cap_usd": "hundred"}, bad("llm.cap_usd"), id="cap word"),
    C({"evaluation.t2_straddle_max_coverage": 0.7}, bad("evaluation.t2_straddle_max_coverage"), id="float"),
    # strict JSON types and unknown keys
    C({"schedule.interval_hours": "6"}, bad("schedule.interval_hours"), id="interval as string"),
    C({"retention.score_history_months": 12.0}, bad("retention.score_history_months"), id="months as float"),
    C({"schedule.enabled": 1}, bad("schedule.enabled"), id="enabled as 1"),
    C({"colour": "blue"}, bad("colour"), id="unknown top-level key"),
    # a NUL character in any string, at its path (no JSONB column stores one)
    C({"scope.excluded_keywords": ["Prak\x00tikum"]}, bad("scope.excluded_keywords[0]"), id="NUL keyword"),
    C(
        {"scope.locations": [loc(1, label="Muster\x00stadt")]},
        bad("scope.locations[0].label"),
        id="NUL label",
    ),
    C({"scope.excluded_companies": ["Muster\x00AG"]}, bad("scope.excluded_companies[0]"), id="NUL company"),
    C({"scope.industries_include": ["Lo\x00gistik"]}, bad("scope.industries_include[0]"), id="NUL industry"),
    C(
        {"scope.excluded_keywords": ["Prak\x00tikum"], "schedule.interval_hours": 0},
        [
            ("payload.schedule.interval_hours", "out_of_range"),
            ("payload.scope.excluded_keywords[0]", "invalid"),
        ],
        id="NUL beside a Pydantic error",
    ),
    C({"schedule.anchor_local": "06:\x000"}, bad("schedule.anchor_local"), id="NUL in a refused field, once"),
    C(
        {"scope.excluded_keywords": ["Prak\x00tikum"], "evaluation.t3_min_mid": 60},
        [("payload.scope.excluded_keywords[0]", "invalid"), ("payload.evaluation.t3_min_mid", "conflict")],
        id="NUL before the cross-field rules",
    ),
    # a lone surrogate (a JSON escape `\ud800`, which no UTF-8 text holds) in any string, at its path: final
    # review FR-1 (b); Pydantic itself refuses one only in a constrained string (the keywords)
    C(
        {"scope.industries_include": ["Abc\ud800xyz"]},
        bad("scope.industries_include[0]"),
        id="surrogate industry",
    ),
    C({"scope.industries_exclude": ["Abc\udfffxyz"]}, bad("scope.industries_exclude[0]"), id="surrogate"),
    C(
        {"scope.locations": [loc(1, label="Muster\ud800stadt")]},
        bad("scope.locations[0].label"),
        id="surrogate label",
    ),
    C(
        {"scope.excluded_companies": ["Muster\ud800AG"]},
        bad("scope.excluded_companies[0]"),
        id="surr. company",
    ),
    C(
        {"scope.excluded_keywords": ["Prak\ud800tikum"]},
        bad("scope.excluded_keywords[0]"),
        id="surr. keyword",
    ),
    C({"scope.locations": {"label": "Musterstadt"}}, bad("scope.locations"), id="locations not a list"),
]


def test_a_lone_surrogate_never_reaches_the_effect_classification() -> None:
    """Final review FR-1 (b): before the fix the industry passed validation and `classify_effects` raised
    `UnicodeEncodeError` on it; now the payload has no settings and one `invalid` issue."""
    settings, issues = validate_fields(apply({"scope.industries_include": ["Abc\ud800xyz"]}))
    assert settings is None
    assert [(i.field, i.code, i.message) for i in issues] == [
        ("payload.scope.industries_include[0]", "invalid", "must be valid Unicode text")
    ]


@pytest.mark.parametrize(("changes", "expected"), CASES)
def test_field_issues(changes: dict[str, Any], expected: list[tuple[str, str]]) -> None:
    settings, issues = validate_fields(apply(changes))
    assert [(issue.field, issue.code) for issue in issues] == expected
    assert all(isinstance(issue, FieldIssue) and issue.message for issue in issues)
    assert (settings is None) == bool(expected)


@pytest.mark.parametrize("payload", [[], "settings", None, 7])
def test_a_payload_that_is_no_object_is_invalid(payload: Any) -> None:
    settings, issues = validate_fields(payload)
    assert settings is None and [(i.field, i.code) for i in issues] == [("payload", "invalid")]


def test_independent_issues_are_all_reported() -> None:
    _, issues = validate_fields(
        apply(
            {
                "scope.remote_within_germany": False,
                "evaluation.t3_min_mid": 60,
                "scoring.execution.critic.max_tokens_retry": 5000,
                "scoring.batch_max_items": 10_000,
            }
        )
    )
    assert [(i.field, i.code) for i in issues] == [
        ("payload.scope.locations", "conflict"),
        ("payload.evaluation.t3_min_mid", "conflict"),
        ("payload.scoring.execution.critic.max_tokens_retry", "conflict"),
        ("payload.llm.overrun_buffer_usd", "out_of_range"),
    ]
    _, field_errors = validate_fields(apply({"schedule.interval_hours": 0, "llm.cap_usd": "0.50"}))
    assert [(i.field, i.code) for i in field_errors] == [
        ("payload.schedule.interval_hours", "out_of_range"),
        ("payload.llm.cap_usd", "out_of_range"),
    ]


def test_the_buffer_issue_names_the_bound() -> None:
    _, issues = validate_fields(apply({"scoring.batch_max_items": 10_000}))
    assert len(issues) == 1 and "23.53" in issues[0].message


def test_an_effort_the_model_does_not_support(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every effort applies to every model today (`pricing.SUPPORTED_EFFORTS`); the per-model check reads the
    # table, so a model that lost an effort is refused at its node and at the critic's light effort.
    narrowed: dict[Model, frozenset[Effort]] = {
        **pricing.SUPPORTED_EFFORTS,
        HAIKU: frozenset({"low", "medium"}),
    }
    monkeypatch.setattr(pricing, "SUPPORTED_EFFORTS", MappingProxyType(narrowed))
    _, issues = validate_fields(
        apply(
            {
                "scoring.model_policy.nodes.generate": {"model": HAIKU, "effort": "max"},
                "scoring.model_policy.nodes.critic": {"model": HAIKU, "effort": "medium"},
                "scoring.model_policy.critic_light_effort": "high",
            }
        )
    )
    assert [(i.field, i.code) for i in issues] == [
        ("payload.scoring.model_policy.nodes.generate.effort", "unsupported"),
        ("payload.scoring.model_policy.critic_light_effort", "unsupported"),
    ]


def low_generator() -> dict[str, Any]:
    return apply({"scoring.model_policy.nodes.generate": {"model": SONNET, "effort": "low"}})


def test_generator_effort_low_needs_a_passing_or_uncalibrated_gate() -> None:
    assert validate_fields(low_generator(), gate="passed")[1] == []
    assert validate_fields(low_generator(), gate="uncalibrated")[1] == []
    assert validate_fields(low_generator())[1] == []  # the gate defaults to uncalibrated (M2)
    settings, issues = validate_fields(low_generator(), gate="failed")
    assert settings is None
    assert [(i.field, i.code) for i in issues] == [
        ("payload.scoring.model_policy.nodes.generate.effort", "conflict")
    ]
    # only the generator is gated (§11.13)
    critic_low = apply({"scoring.model_policy.nodes.critic": {"model": OPUS, "effort": "low"}})
    assert validate_fields(critic_low, gate="failed")[1] == []


def test_a_failed_gate_refuses_selecting_generator_low_not_keeping_it() -> None:
    """Ruling T20-d (L3827, "low can be selected only after the gate shows parity"): with the gate failed,
    a document that changes the generator's effort to `low` is refused; one that carries the active `low`
    unchanged is valid, with the warning `eval_failed`."""
    active_low = Settings.model_validate(low_generator())
    active_medium = Settings()
    assert validate_fields(low_generator(), gate="failed", active=active_low) == (active_low, [])
    _, issues = validate_fields(low_generator(), gate="failed", active=active_medium)
    assert [(i.field, i.code) for i in issues] == [
        ("payload.scoring.model_policy.nodes.generate.effort", "conflict")
    ]
    kept = Settings.model_validate(low_generator())
    assert warnings_only(kept, gate="failed", active=active_low) == [Warning("eval_failed", node="generate")]
    assert warnings_only(kept, gate="passed", active=active_low) == []
    # below 30 cases M2's warning, whether selected or kept
    assert warnings_only(kept, gate="uncalibrated", active=active_low) == [
        Warning("eval_uncalibrated", node="generate")
    ]


def test_warnings_only() -> None:
    assert warnings_only(Settings(), gate="uncalibrated") == []
    low = Settings.model_validate(low_generator())
    assert warnings_only(low, gate="uncalibrated") == [Warning("eval_uncalibrated", node="generate")]
    assert warnings_only(low, gate="passed") == []
    no_serpapi = Settings.model_validate(apply({"sources.enabled.serpapi": False}))
    assert warnings_only(no_serpapi, gate="uncalibrated") == [Warning("serpapi_disabled_no_fallback")]
    both = Settings.model_validate({**low_generator(), "sources": no_serpapi.to_payload()["sources"]})
    assert [w.code for w in warnings_only(both, gate="uncalibrated")] == [
        "eval_uncalibrated",
        "serpapi_disabled_no_fallback",
    ]


def test_the_gate_accepts_fake_runs_only_in_a_fake_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex r2 B20 (M3 Task 20): the gate reads live runs, and fake ones only while this process runs on the
    fake LLM: `JOBHUNTER_FAKE_LLM` set to anything, as `config.load_config` reads it. (The gate's reads of
    the golden cases and the runs are `tests/integration/test_eval_gate.py`'s.)"""
    monkeypatch.delenv("JOBHUNTER_FAKE_LLM", raising=False)
    assert (fake_llm_process(), gate_transports()) == (False, ("live",))
    monkeypatch.setenv("JOBHUNTER_FAKE_LLM", "")
    assert (fake_llm_process(), gate_transports()) == (True, ("live", "fake"))


def at_items(items: int, buffer: str = "50.00") -> Settings:
    return Settings.model_validate(
        apply({"scoring.batch_max_items": items, "llm.overrun_buffer_usd": buffer})
    )


def test_the_overrun_buffer_bound_figures() -> None:
    # The M3 re-count (V19, 1,878 tokens): 5 x 1,878 x $5.00 + 500 x 1,878 x $1.25 = $1.2207, rounded up to
    # $1.23; 5 x 1,878 x $5.00 + 10,000 x 1,878 x $1.25 = $23.52195, rounded up to $23.53
    assert overrun_buffer_bound(Settings()) == Decimal("1.23")
    assert overrun_buffer_bound(at_items(10_000)) == Decimal("23.53")


def test_the_bound_figures_follow_the_overhead_and_the_prices() -> None:
    # Derived from `bounds` and `pricing`: a change of either constant fails the first assert with the four
    # inputs, and the formula's figures against the pinned literals in the second.
    overhead = bounds.PROMPT_OVERHEAD_TOKENS
    online = pricing.bound_input_price(OPUS, batch=False)
    batch = pricing.bound_input_price(SONNET, batch=True)
    assert (overhead[OPUS], online, overhead[SONNET], batch) == (
        1_878,
        Decimal("5.00"),
        1_878,
        Decimal("1.25"),
    )

    def bound(items: int) -> Decimal:
        total = 5 * overhead[OPUS] * online + items * overhead[SONNET] * batch
        return (total / 1_000_000).quantize(Decimal("0.01"), rounding=ROUND_CEILING)

    assert (bound(500), bound(10_000)) == (Decimal("1.23"), Decimal("23.53"))
    assert (overrun_buffer_bound(Settings()), overrun_buffer_bound(at_items(10_000))) == (
        bound(500),
        bound(10_000),
    )


def test_the_bound_takes_the_costliest_online_model_and_the_generators_batch_price() -> None:
    # Fable as the critic: 5 online slots at 1,878 x $12.50, the batch items still at Sonnet's $1.25:
    # (117,375 + 1,173,750) / 1e6 = 1.291125 -> $1.30
    fable_critic = apply({"scoring.model_policy.nodes.critic": {"model": FABLE, "effort": "medium"}})
    assert overrun_buffer_bound(Settings.model_validate(fable_critic)) == Decimal("1.30")
    # Haiku as the generator: batch items at Haiku's top-tier batch price $0.3125, the online slots at Opus:
    # (46,950 + 500 x 1,878 x 0.3125) / 1e6 = 0.3403875 -> $0.35
    haiku_generator = apply({"scoring.model_policy.nodes.generate": {"model": HAIKU, "effort": "medium"}})
    assert overrun_buffer_bound(Settings.model_validate(haiku_generator)) == Decimal("0.35")
    # rounded up to the cent, never to the nearest: Haiku everywhere and 200 batch items give
    # (5 x 1,878 x $0.625 + 200 x 1,878 x $0.3125) / 1e6 = 0.12324375 -> $0.13
    haiku = apply({"scoring.model_policy.nodes": HAIKU_NODES, "scoring.batch_max_items": 200})
    assert overrun_buffer_bound(Settings.model_validate(haiku)) == Decimal("0.13")
    # the overhead constants may be given (the V19 spike passes its own); 1,000 tokens everywhere and 1,000
    # items is §6.9.1's former example: 5 x 1,000 x $5.00 + 1,000 x 1,000 x $1.25 -> $1.28
    floor = dict.fromkeys(pricing.MODELS, 1_000)
    assert overrun_buffer_bound(at_items(1_000), overhead=floor) == Decimal("1.28")
