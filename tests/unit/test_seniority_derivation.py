"""The effective seniority range (spec §12.4, §13.1 `scope.seniority_range`, plan Deviation 14; Task 15).

`derived_seniority(profile_seniority, below=, above=)` gives the explicit `(min, max)` on
`normalize.titles.SENIORITY_LADDER` (the §12.4 enum in ladder order), clamped at both ends; `derived(settings,
profile=…)` resolves a `profile_relative` range with it, and keeps an `explicit` one, or a `profile_relative`
one without a profile, as the settings hold it.
"""

import copy
from typing import Any

import pytest
from jobhunter.db.seed import load_settings_v1, scope_payload
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.normalize.titles import SENIORITY_LADDER
from jobhunter.settings.derive import derived, derived_seniority
from jobhunter.settings.model import Settings
from jobhunter.types import get_object


def profile(seniority: str) -> ConfirmedProfile:
    """A confirmed profile of an invented candidate: only its seniority matters here."""
    return ConfirmedProfile.model_validate(
        {
            "summary": "Backend-Entwicklerin mit Schwerpunkt auf Python-Services.",
            "target_titles": [{"title": "Backend Developer", "lang": "en"}],
            "seniority": seniority,
            "total_years_experience": 4.0,
            "roles": [],
            "skills": [],
            "languages": [],
            "education": [],
            "certifications": [],
            "domains": [],
        }
    )


@pytest.mark.parametrize(
    ("level", "below", "above", "expected"),
    [
        ("mid", 1, 1, ("junior", "senior")),
        ("mid", 0, 0, ("mid", "mid")),
        ("mid", 3, 3, ("working_student", "principal")),
        ("junior", 2, 0, ("working_student", "junior")),
        ("intern", 3, 1, ("intern", "working_student")),  # clamped at the bottom of the ladder
        ("working_student", 3, 0, ("intern", "working_student")),
        ("head", 1, 3, ("principal", "head")),  # clamped at the top
        ("principal", 0, 3, ("principal", "head")),
        ("intern", 0, 3, ("intern", "junior")),
        ("head", 3, 0, ("senior", "head")),
    ],
)
def test_the_profile_level_minus_below_and_plus_above(
    level: str, below: int, above: int, expected: tuple[str, str]
) -> None:
    assert derived_seniority(level, below=below, above=above) == expected


def test_an_intern_with_three_below_has_intern_as_its_minimum() -> None:
    low, _ = derived_seniority("intern", below=3, above=1)
    assert low == "intern" == SENIORITY_LADDER[0]


@pytest.mark.parametrize("level", ["unspecified", "Senior", "boss", ""])
def test_a_level_off_the_ladder_is_refused(level: str) -> None:
    with pytest.raises(ValueError, match="seniority"):
        derived_seniority(level, below=1, above=1)


def test_a_negative_distance_is_refused() -> None:
    with pytest.raises(ValueError, match="below"):
        derived_seniority("mid", below=-1, above=1)


def with_range(seniority_range: dict[str, Any]) -> Settings:
    doc: dict[str, Any] = copy.deepcopy(load_settings_v1())
    doc["scope"]["seniority_range"] = seniority_range
    return Settings.model_validate(doc)


def test_a_profile_resolves_a_profile_relative_range() -> None:
    settings = with_range({"mode": "profile_relative", "below": 1, "above": 2, "min": None, "max": None})
    scope = derived(settings, profile=profile("mid")).scope
    assert scope["seniority_range"] == {
        "mode": "explicit",
        "below": 1,
        "above": 2,
        "min": "junior",
        "max": "lead",
    }
    # the rest of the scope is the settings' own (§13.1 group Scope, without the admission field)
    assert {k: v for k, v in scope.items() if k != "seniority_range"} == {
        k: v for k, v in scope_payload(settings.to_payload()).items() if k != "seniority_range"
    }
    assert "max_ad_age_days" not in scope


def test_without_a_profile_the_range_stays_profile_relative() -> None:
    settings = Settings()
    assert derived(settings).scope == scope_payload(load_settings_v1())
    assert get_object(derived(settings).scope, "seniority_range")["mode"] == "profile_relative"


def test_an_explicit_range_ignores_the_profile() -> None:
    explicit = {"mode": "explicit", "below": 1, "above": 1, "min": "senior", "max": "head"}
    settings = with_range(explicit)
    assert derived(settings, profile=profile("junior")).scope["seniority_range"] == explicit
    assert derived(settings, profile=profile("junior")) == derived(settings)


def test_the_derived_payloads_of_the_defaults_are_the_seeded_rows() -> None:
    # The seed derives the four rows of version 1 from the same objects (`db.seed._insert_settings_v1`).
    seeded = load_settings_v1()
    d = derived(Settings())
    assert d.scope == scope_payload(seeded)
    assert d.model_policy == get_object(seeded, "scoring.model_policy")
    assert d.execution == get_object(seeded, "scoring.execution")
    assert d.evaluation == get_object(seeded, "evaluation")
