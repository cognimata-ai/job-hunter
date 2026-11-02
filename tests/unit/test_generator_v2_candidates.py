"""scripts/spikes/generator_v2_candidates.py and the v2probe spike module (M3 pre-plan spike V4b): the three
candidate generator wire schemas of prompt-schema version 2 and the probe's ladder.

The counts are pinned to the table in A3 §3.4 (the M3 scoring research). The walk must first reproduce A3's
rows for the four version-1 schemas, so a change to the walker cannot hide a change to a candidate.
"""

import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import anthropic
import pytest
from jobhunter.llm import fake
from jobhunter.llm.schemas import SCHEMAS_V1, transformed

REPO = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts/spikes" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CANDIDATES = _load("generator_v2_candidates")
PROBE = _load("v4b_generator_v2_probe")

# A3 §3.4: bytes, objects, properties, arrays, enum positions (values), anyOf unions, free strings,
# booleans, integers/numbers, optional properties, depth. Every count but the canonical bytes is ref-expanded.
A3_TABLE: dict[str, dict[str, int]] = {
    "v1": dict(
        canonical_bytes=5_621,
        expanded_bytes=8_824,
        objects=29,
        properties=105,
        arrays=32,
        enums=25,
        enum_values=154,
        unions=11,
        free_strings=42,
        booleans=9,
        numbers=1,
        optional=0,
        depth=5,
    ),
    "a": dict(
        canonical_bytes=5_386,
        expanded_bytes=5_359,
        objects=18,
        properties=65,
        arrays=23,
        enums=11,
        enum_values=75,
        unions=6,
        free_strings=27,
        booleans=9,
        numbers=1,
        optional=0,
        depth=5,
    ),
    "b": dict(
        canonical_bytes=4_653,
        expanded_bytes=4_542,
        objects=18,
        properties=65,
        arrays=23,
        enums=3,
        enum_values=14,
        unions=1,
        free_strings=35,
        booleans=9,
        numbers=1,
        optional=0,
        depth=5,
    ),
    "c": dict(
        canonical_bytes=1_713,
        expanded_bytes=1_613,
        objects=4,
        properties=19,
        arrays=5,
        enums=4,
        enum_values=30,
        unions=1,
        free_strings=9,
        booleans=3,
        numbers=0,
        optional=0,
        depth=5,
    ),
}
# A3's rows of the three schemas the API accepted in V4.
A3_ACCEPTED: dict[str, tuple[int, int, int, int, int, int, int]] = {
    # canonical bytes, expanded bytes, objects, properties, arrays, enum positions, unions
    "critic": (952, 874, 2, 9, 2, 4, 1),
    "profile": (2_906, 2_906, 7, 35, 8, 9, 11),
    "queries": (414, 414, 2, 6, 1, 1, 0),
}
BYTES_TOLERANCE = 0.01  # a cosmetic change to a candidate may move its bytes by 1%, and no count at all


def _schema(name: str) -> dict[str, Any]:
    if name == "v1":
        schema: dict[str, Any] = CANDIDATES.v1()
        return schema
    built: dict[str, Any] = CANDIDATES.CANDIDATES[name]()
    return built


@pytest.mark.parametrize("name", list(A3_TABLE))
def test_each_schema_has_a3s_structural_counts(name: str) -> None:
    got = vars(CANDIDATES.counts(_schema(name)))
    want = A3_TABLE[name]
    for column in ("canonical_bytes", "expanded_bytes"):
        assert abs(got[column] - want[column]) <= want[column] * BYTES_TOLERANCE, column
    exact = {k: v for k, v in want.items() if not k.endswith("_bytes")}
    assert {k: got[k] for k in exact} == exact


@pytest.mark.parametrize("name", list(A3_ACCEPTED))
def test_the_walk_reproduces_a3s_rows_of_the_accepted_v1_schemas(name: str) -> None:
    c = CANDIDATES.counts(transformed(SCHEMAS_V1[name]))
    got = (c.canonical_bytes, c.expanded_bytes, c.objects, c.properties, c.arrays, c.enums, c.unions)
    assert got == A3_ACCEPTED[name]


def test_v1_is_the_schema_the_gateway_sends_for_the_generator() -> None:
    assert CANDIDATES.v1() == transformed(SCHEMAS_V1["generator"])


@pytest.mark.parametrize("name", ["v1", "a", "b", "c"])
def test_each_schema_is_a_fixed_point_of_the_sdk_transform(name: str) -> None:
    schema = _schema(name)
    out = anthropic.transform_schema(copy.deepcopy(schema))
    assert out == schema
    assert fake.canonical_schema_json(out) == fake.canonical_schema_json(schema)
    assert CANDIDATES.is_transform_fixed_point(schema)


def test_a_transform_that_rewrites_a_schema_is_no_fixed_point() -> None:
    schema = CANDIDATES.candidate_c()
    schema["$defs"]["fact"]["properties"]["value"]["maxLength"] = 120  # moved into a description
    assert not CANDIDATES.is_transform_fixed_point(schema)


def _paths(node: Any, prefix: str = "") -> dict[str, Any]:
    """Every leaf of `node` by its path."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            out.update(_paths(value, f"{prefix}/{key}"))
        return out or {prefix: {}}
    return {prefix: json.dumps(node)}


def _changed(old: dict[str, Any], new: dict[str, Any]) -> set[str]:
    before, after = _paths(old), _paths(new)
    return {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}


def test_a_changes_only_criteria_and_the_criterion_item() -> None:
    a = CANDIDATES.candidate_a()
    changed = _changed(CANDIDATES.v1(), a)
    assert changed and all(
        p.startswith(
            ("/properties/criteria", "/$defs/criterion/properties/criterion", "/$defs/criterion/required")
        )
        for p in changed
    )
    assert a["properties"]["criteria"] == {"type": "array", "items": {"$ref": "#/$defs/criterion"}}
    criterion = a["$defs"]["criterion"]
    assert criterion["properties"]["criterion"] == {"type": "string", "enum": list(CANDIDATES.CRITERION_KEYS)}
    assert criterion["required"] == [
        "criterion",
        "level",
        "basis",
        "ad_evidence",
        "profile_evidence",
        "rationale",
    ]


def test_b_changes_only_the_vocabularies_and_the_nullables_of_a() -> None:
    a, b = CANDIDATES.candidate_a(), CANDIDATES.candidate_b()
    vocabularies = ["/$defs/" + "/".join(path) for path in CANDIDATES.B_VOCABULARIES]
    sentinels = [f"/$defs/{name}" for name in CANDIDATES.B_SENTINELS]
    assert all(p.startswith(tuple(vocabularies + sentinels)) for p in _changed(a, b))
    for path in CANDIDATES.B_VOCABULARIES:
        node = b["$defs"]
        for key in path:
            node = node[key]
        assert node == {"type": "string"}
    assert b["$defs"]["nullable_string"] == {"type": "string"}
    assert b["$defs"]["nullable_int"] == {"type": "integer"}
    # only the criterion key, the basis and the level stay constrained
    criterion = b["$defs"]["criterion"]["properties"]
    assert criterion["criterion"]["enum"] == list(CANDIDATES.CRITERION_KEYS)
    assert b["$defs"]["level"] == a["$defs"]["level"]


def test_c_carries_the_same_criterion_keys_and_level_as_a() -> None:
    a, c = CANDIDATES.candidate_a(), CANDIDATES.candidate_c()
    criterion = c["$defs"]["criterion"]["properties"]
    assert criterion["criterion"] == a["$defs"]["criterion"]["properties"]["criterion"]
    assert criterion["level"] == a["$defs"]["level"]
    assert criterion["basis"] == a["$defs"]["criterion"]["properties"]["basis"]
    assert c["required"] == [
        "not_a_job_ad",
        "injection_observed",
        "facts",
        "criteria",
        "summary_en",
        "summary_de",
    ]


def test_each_builder_returns_a_fresh_schema() -> None:
    for build in CANDIDATES.CANDIDATES.values():
        first = build()
        first["properties"].clear()
        assert build()["properties"]
    assert list(CANDIDATES.CANDIDATES) == ["a", "b", "c"]


def test_the_walk_refuses_a_recursive_ref() -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/x"}},
        "$defs": {"x": {"$ref": "#/$defs/x"}},
    }
    with pytest.raises(ValueError, match="recursive"):
        CANDIDATES.counts(schema)


# ---- the probe module -------------------------------------------------------------------------------------


def test_the_probe_sends_each_candidate_once_on_sonnet_low_with_a_small_max_tokens() -> None:
    calls = PROBE.calls()
    assert [c["case"] for c in calls] == ["candidate_a", "candidate_b", "candidate_c"]
    for call, label in zip(calls, "abc", strict=True):
        assert (call["model"], call["effort"], call["max_tokens"], call["purpose"]) == (
            "claude-sonnet-5-5",
            "low",
            1024,
            "generate",
        )
        sent = anthropic.transform_schema(call["schema"])  # as the gateway and langchain transform it
        assert sent == CANDIDATES.CANDIDATES[label]() and "title" not in sent


def test_the_probe_prompt_is_a_short_synthetic_ad_and_profile() -> None:
    (call, *_) = PROBE.calls()
    user = call["user"]
    ad = user.split("<job_ad>\n")[1].split("\n</job_ad>")[0]
    profile = user.split("<candidate_profile>\n")[1].split("\n</candidate_profile>")[0]
    assert len(ad.splitlines()) == 3 and len(profile.splitlines()) == 3
    assert user.startswith("Rate this synthetic job ad against this synthetic profile.")
    assert "@" not in user and "synthetic" in call["system"][0]


ACCEPTED, REJECTED, UNEXPECTED = "accepted", "rejected", "unexpected"


def _v(**verdicts: str) -> dict[str, str]:
    return {f"candidate_{label}": verdict for label, verdict in verdicts.items()}


@pytest.mark.parametrize(
    ("verdicts", "sent_next", "chosen"),
    [
        (_v(), ["candidate_a"], None),
        (_v(a=ACCEPTED), ["candidate_c"], "a"),  # C once, as the fallback record; B never
        (_v(a=ACCEPTED, c=REJECTED), [], "a"),
        (_v(a=ACCEPTED, c=ACCEPTED), [], "a"),  # A whenever A and C both compile
        (_v(a=REJECTED), ["candidate_b"], None),
        (_v(a=REJECTED, b=ACCEPTED), [], "b"),
        (_v(a=REJECTED, b=REJECTED), ["candidate_c"], None),
        (_v(a=REJECTED, b=REJECTED, c=ACCEPTED), [], "c"),
        (_v(a=REJECTED, b=REJECTED, c=REJECTED), [], None),
        (_v(a=UNEXPECTED), [], None),
        (_v(a=REJECTED, b=UNEXPECTED), [], None),
    ],
)
def test_the_ladder_sends_only_what_it_needs_and_chooses_the_smallest_change(
    verdicts: dict[str, str], sent_next: list[str], chosen: str | None
) -> None:
    cases = [c["case"] for c in PROBE.calls()]
    assert [c for c in cases if c not in verdicts and PROBE.wanted(c, verdicts)] == sent_next
    assert PROBE.chosen(verdicts) == chosen


def _request(label: str) -> dict[str, Any]:
    return {"output_config": {"format": {"type": "json_schema", "schema": CANDIDATES.CANDIDATES[label]()}}}


GRAMMAR_MESSAGE = (
    "The compiled grammar is too large, which would cause performance issues. Simplify your tool schemas or "
    "reduce the number of strict tools."
)


def _error(label: str, status: int, message: str, sent: str | None = None) -> dict[str, Any]:
    return {
        "requested_model": "claude-sonnet-5-5",
        "transformed_schema": CANDIDATES.CANDIDATES[label](),
        "request": _request(sent or label),
        "status": status,
        "error_type": "invalid_request_error",
        "error_message": message,
    }


def test_a_grammar_too_large_400_is_a_rejection_and_any_other_error_is_unexpected() -> None:
    rejected = PROBE.check(_error("a", 400, GRAMMAR_MESSAGE))
    assert (rejected["verdict"], rejected["expected"], rejected["candidate"]) == (REJECTED, True, "a")
    assert rejected["error_message"] == GRAMMAR_MESSAGE
    for observation in (
        _error("a", 400, "Schema is too complex for compilation."),
        _error("a", 500, GRAMMAR_MESSAGE),
        _error("a", 400, GRAMMAR_MESSAGE, sent="b"),  # not the planned candidate
    ):
        report = PROBE.check(observation)
        assert (report["verdict"], report["expected"]) == (UNEXPECTED, False)


def _stream(
    label: str, text: str, *, model: str = "claude-sonnet-5-5", stop: str = "end_turn"
) -> dict[str, Any]:
    events = [
        {"name": "content_block_start", "data": {"index": 0, "content_block": {"type": "text", "text": ""}}},
        {"name": "content_block_delta", "data": {"index": 0, "delta": {"type": "text_delta", "text": text}}},
    ]
    return {
        "requested_model": "claude-sonnet-5-5",
        "transformed_schema": CANDIDATES.CANDIDATES[label](),
        "request": _request(label),
        "status": 200,
        "events": events,
        "response_metadata": {"model_name": model, "stop_reason": stop},
        "usage_metadata": {"input_tokens": 3_000, "output_tokens": 1_024},
        "parsed_ok": False,
        "parsing_error": "OutputParserException",
    }


def test_a_200_is_an_acceptance_whatever_its_stop_reason_and_keeps_no_content() -> None:
    cut = PROBE.check(
        _stream("c", '{"not_a_job_ad": false, "summary_en": "Secret-ish text', stop="max_tokens")
    )
    assert (cut["verdict"], cut["expected"], cut["stop_reason"]) == (ACCEPTED, True, "max_tokens")
    assert cut["output_complete_json"] is False and cut["violations"] is None
    assert cut["usage"] == {"input_tokens": 3_000, "output_tokens": 1_024}
    assert "Secret-ish" not in json.dumps(cut)
    whole = PROBE.check(_stream("c", json.dumps({"not_a_job_ad": "no"})))
    assert whole["verdict"] == ACCEPTED and whole["output_complete_json"] is True
    assert whole["violations"]  # paths only: the informational check of the output against the candidate
    other_model = PROBE.check(_stream("c", "{}", model="claude-haiku-5-5"))
    assert (other_model["verdict"], other_model["expected"]) == (UNEXPECTED, False)
