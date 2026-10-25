"""`llm.schema_dry_run.schema_violations` (spec §10.11; plan Task 7, review fix 1): the paths where an output
departs from the schema the prompt-schema version stores. On the critic schema (§11.7.3), each kind of
departure is reported at its path, and a valid body reports none."""

import copy
from typing import Any

import pytest
from jobhunter.llm.schema_dry_run import schema_violations
from jobhunter.llm.schemas import SCHEMAS, transformed

CRITIC = transformed(SCHEMAS["critic"])


def valid() -> dict[str, Any]:
    return {
        "verdict": "revise",
        "reviews": [
            {
                "target": "skills",
                "assessment": "level_too_high",
                "suggested_level": 2,
                "ad_evidence": ["4+ Jahre Python"],
                "explanation": "The ad asks for more years.",
            },
            {
                "target": "languages",
                "assessment": "fact_confirmed",
                "suggested_level": None,
                "ad_evidence": [],
                "explanation": "Confirmed.",
            },
        ],
        "injection": "none",
        "summary": "One level is too high.",
    }


def with_(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    body = valid()
    node: Any = body
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return body


def test_a_valid_body_reports_no_violation() -> None:
    assert schema_violations(CRITIC, valid()) == []


@pytest.mark.parametrize(
    ("body", "path"),
    [
        pytest.param(with_(("verdict",), "maybe"), "$.verdict", id="out-of-enum, top level"),
        pytest.param(
            with_(("reviews", 0, "assessment"), "looks_fine"),
            "$.reviews[0].assessment",
            id="out-of-enum, nested",
        ),
        pytest.param(
            with_(("reviews", 1, "suggested_level"), 5),
            "$.reviews[1].suggested_level",
            id="out-of-range level",
        ),
        pytest.param(
            with_(("reviews", 0, "suggested_level"), True),
            "$.reviews[0].suggested_level",
            id="True for an int",
        ),
        pytest.param(
            with_(("reviews", 0, "suggested_level"), "2"),
            "$.reviews[0].suggested_level",
            id="no anyOf branch",
        ),
        pytest.param(with_(("reviews",), {"0": {}}), "$.reviews", id="an object for an array"),
        pytest.param(with_(("summary",), 7), "$.summary", id="a number for a string"),
    ],
)
def test_each_wrong_value_is_reported_at_its_path(body: dict[str, Any], path: str) -> None:
    assert schema_violations(CRITIC, body) == [path]


def test_a_missing_required_key_is_reported() -> None:
    body = valid()
    del body["summary"]
    del body["reviews"][1]["explanation"]
    assert sorted(schema_violations(CRITIC, body)) == ["$.reviews[1].explanation", "$.summary"]


def test_an_unknown_key_is_reported() -> None:
    body = valid()
    body["confidence"] = 0.9  # additionalProperties: false
    body["reviews"][0]["note"] = "extra"
    assert sorted(schema_violations(CRITIC, body)) == ["$.confidence", "$.reviews[0].note"]


def test_a_ref_that_resolves_to_no_definition_is_reported() -> None:
    schema: dict[str, Any] = copy.deepcopy(CRITIC)
    schema["properties"]["reviews"]["items"] = {"$ref": "#/$defs/no_such_definition"}
    assert schema_violations(schema, valid()) == ["$.reviews[0]", "$.reviews[1]"]
    schema["properties"]["reviews"]["items"] = {
        "$ref": "https://example.invalid/review"
    }  # not a local $defs ref
    assert schema_violations(schema, valid()) == ["$.reviews[0]", "$.reviews[1]"]


def test_an_anyof_that_no_branch_matches_is_reported() -> None:
    # the level definition is `anyOf: [integer in 0..4, null]`: a float matches neither branch
    assert schema_violations(CRITIC, with_(("reviews", 0, "suggested_level"), 2.5)) == [
        "$.reviews[0].suggested_level"
    ]


def test_several_violations_are_all_reported_and_name_no_value() -> None:
    body = with_(("verdict",), "Beispiel-Wert")
    body["reviews"][0]["assessment"] = "geheim"
    found = schema_violations(CRITIC, body)
    assert sorted(found) == ["$.reviews[0].assessment", "$.verdict"]
    assert not any("Beispiel" in p or "geheim" in p for p in found)  # paths only, never content


def test_true_is_neither_an_integer_nor_the_enum_value_1_on_its_own() -> None:
    # On the critic schema the type check and the enum check each catch `True` for a level; here each layer
    # is held alone, so neither can rely on the other.
    assert schema_violations({"type": "integer"}, True) == ["$"]
    assert schema_violations({"type": "number"}, False) == ["$"]
    assert schema_violations({"enum": [0, 1]}, True) == ["$"]
    assert schema_violations({"enum": ["1"]}, 1) == ["$"]
    assert schema_violations({"type": "integer", "enum": [0, 1]}, 1) == []
