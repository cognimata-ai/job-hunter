"""The diff of two settings documents (plan Task 17, `GET /api/settings/versions/{id}`): one entry per changed
leaf, at its `FieldIssue` path; lists of objects with an id key matched by that key, other lists by position;
`None` stands for a side where the value is absent."""

import copy
from typing import Any

from jobhunter.db.seed import load_settings_v1
from jobhunter.settings.effects import DiffEntry, diff_payloads

LOCATION: dict[str, Any] = {
    "label": "Musterstadt",
    "gazetteer_id": "gz-0001",
    "lat": 52.1,
    "lon": 9.9,
    "radius_km": 25,
}
OTHER: dict[str, Any] = {
    "label": "Beispielheim",
    "gazetteer_id": "gz-0002",
    "lat": 50.0,
    "lon": 8.0,
    "radius_km": 10,
}


def seed() -> dict[str, Any]:
    doc: dict[str, Any] = copy.deepcopy(load_settings_v1())
    return doc


def test_identical_documents_have_no_diff() -> None:
    assert diff_payloads(seed(), seed()) == []
    assert diff_payloads({}, {}) == [] and diff_payloads([], []) == []


def test_each_changed_leaf_is_named_by_its_path_in_document_order() -> None:
    after = seed()
    after["schedule"]["interval_hours"] = 12
    after["scope"]["knockout_severity"]["degree"] = "hard"
    after["llm"]["cap_usd"] = "150.00"
    assert diff_payloads(seed(), after) == [
        DiffEntry("payload.schedule.interval_hours", 6, 12),
        DiffEntry("payload.scope.knockout_severity.degree", "soft", "hard"),
        DiffEntry("payload.llm.cap_usd", "100.00", "150.00"),
    ]


def test_map_keys_added_and_removed() -> None:
    after = seed()
    after["scope"]["language_policy"] = {"fr": "soft"}
    assert diff_payloads(seed(), after) == [DiffEntry("payload.scope.language_policy.fr", None, "soft")]
    assert diff_payloads(after, seed()) == [DiffEntry("payload.scope.language_policy.fr", "soft", None)]


def test_a_bool_is_no_integer_and_null_is_a_value() -> None:
    assert diff_payloads({"a": 1}, {"a": True}) == [DiffEntry("payload.a", 1, True)]
    assert diff_payloads({"a": None}, {"a": 0}) == [DiffEntry("payload.a", None, 0)]
    assert diff_payloads({"a": 1.0}, {"a": 1.5}) == [DiffEntry("payload.a", 1.0, 1.5)]


def test_entries_with_an_id_key_are_matched_by_it() -> None:
    before: dict[str, Any] = {"queries": [{"query_id": 1, "enabled": True}, {"query_id": 2, "enabled": True}]}
    after: dict[str, Any] = {"queries": [{"query_id": 2, "enabled": True}, {"query_id": 1, "enabled": False}]}
    # the move of query 2 is no change; query 1 is reported at its index in `after`
    assert diff_payloads(before, after) == [DiffEntry("payload.queries[1].enabled", True, False)]
    added: dict[str, Any] = {"queries": [*before["queries"], {"query_id": 3, "enabled": True}]}
    assert diff_payloads(before, added) == [
        DiffEntry("payload.queries[2]", None, {"query_id": 3, "enabled": True})
    ]
    removed: dict[str, Any] = {"queries": [{"query_id": 2, "enabled": True}]}
    assert diff_payloads(before, removed) == [
        DiffEntry("payload.queries[0]", {"query_id": 1, "enabled": True}, None)
    ]


def test_watchlist_and_locations_are_matched_by_their_ids() -> None:
    before: dict[str, Any] = {
        "watchlist": [{"watchlist_company_id": 7, "enabled": True}],
        "scope": {"locations": [LOCATION, OTHER]},
    }
    after: dict[str, Any] = {
        "watchlist": [
            {"watchlist_company_id": 8, "enabled": True},
            {"watchlist_company_id": 7, "enabled": True},
        ],
        "scope": {"locations": [{**OTHER, "radius_km": 30}]},
    }
    assert diff_payloads(before, after) == [
        DiffEntry("payload.watchlist[0]", None, {"watchlist_company_id": 8, "enabled": True}),
        DiffEntry("payload.scope.locations[0].radius_km", 10, 30),
        DiffEntry("payload.scope.locations[0]", LOCATION, None),  # removed: its index in `before`
    ]


def test_other_lists_compare_by_position() -> None:
    after = seed()
    after["scope"]["work_modes"] = ["remote", "onsite"]
    after["scope"]["excluded_keywords"] = ["Praktikum"]
    assert diff_payloads(seed(), after) == [
        DiffEntry("payload.scope.work_modes[1]", "hybrid", "onsite"),
        DiffEntry("payload.scope.work_modes[2]", "onsite", None),
        DiffEntry("payload.scope.excluded_keywords[0]", None, "Praktikum"),
    ]


def test_entries_whose_ids_repeat_fall_back_to_positions() -> None:
    before: dict[str, Any] = {
        "queries": [{"query_id": 1, "enabled": True}, {"query_id": 1, "enabled": False}]
    }
    after: dict[str, Any] = {
        "queries": [{"query_id": 1, "enabled": False}, {"query_id": 1, "enabled": False}]
    }
    assert diff_payloads(before, after) == [DiffEntry("payload.queries[0].enabled", True, False)]


def test_a_value_that_changes_its_type_is_one_entry() -> None:
    assert diff_payloads({"a": {"b": 1}}, {"a": [1]}) == [DiffEntry("payload.a", {"b": 1}, [1])]
    assert diff_payloads({"a": "auto"}, {"a": 10}) == [DiffEntry("payload.a", "auto", 10)]
