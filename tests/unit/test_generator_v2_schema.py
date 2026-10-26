"""Prompt-schema version 2's generator wire schema (M3 Task 4): candidate C of the pre-plan probe V4b
(`docs/spikes/V4b-generator-v2.md`, accepted on `claude-sonnet-5-5`), its structural mirror `GeneratorWire`,
the internal draft shape `GeneratorDraft`, and the two frozen prompt-schema versions.

- `json_schema(GeneratorWire)` is candidate C byte for byte as canonical JSON, with no `title`, and a fixed
  point of `anthropic.transform_schema`, so a request sends exactly the stored schema.
- Version 1 is frozen: `prompt_schema_v1()` reads only `TEMPLATE_NAMES_V1` and `SCHEMAS_V1`, so its hash is
  the literal the M2 seed wrote. Version 2 keeps version 1's two templates and three of its schemas.
- The parser family is a stored constant; `mirror_for` names the structural mirror of each.

The candidate module is loaded by path (`scripts` is not a package and mypy does not cover it).
"""

import copy
import importlib.util
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, get_args

import anthropic
import pytest
from jobhunter.db.seed import canonical_json
from jobhunter.llm.prompts.render import (
    RENDERER_VERSION,
    TEMPLATE_NAMES_V1,
    TEMPLATE_NAMES_V2,
    prompt_schema_hash,
    prompt_schema_v1,
    prompt_schema_v2,
)
from jobhunter.llm.schemas import (
    PARSER_FAMILIES,
    SCHEMAS_V1,
    SCHEMAS_V2,
    ParserFamily,
    json_schema,
    mirror_for,
    transformed,
)
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.generator import (
    CriterionKey,
    FactItemCode,
    GeneratorDraft,
    GeneratorOut,
    GeneratorWire,
)
from jobhunter.llm.schemas.profile import ProfileOut
from jobhunter.llm.schemas.queries import QueryProposalOut
from pydantic import ValidationError

from tests.support.fixtures import _SEEDED_PROMPT_SCHEMA_HASH_V1, _SEEDED_PROMPT_SCHEMA_HASH_V2
from tests.support.llm import generator_draft_body, generator_wire_body

REPO = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts/spikes" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


candidate_c = _load("generator_v2_candidates").candidate_c


def test_the_generator_wire_schema_is_candidate_c_byte_for_byte() -> None:
    assert SCHEMAS_V2["generator"] is GeneratorWire and SCHEMAS_V1["generator"] is GeneratorOut
    assert canonical_json(transformed(GeneratorWire)) == canonical_json(candidate_c())
    assert b'"title"' not in canonical_json(transformed(GeneratorWire))


def test_the_wire_schema_is_a_fixed_point_of_the_sdk_transform() -> None:
    exported = json_schema(GeneratorWire)
    assert anthropic.transform_schema(copy.deepcopy(exported)) == exported
    assert transformed(GeneratorWire) == exported
    # the SDK's transform of the mirror class itself (langchain's path for a model class) is the same schema
    assert anthropic.transform_schema(GeneratorWire) == exported


def test_the_defs_are_the_candidates_names() -> None:
    schema = transformed(GeneratorWire)
    defs = schema["$defs"]
    assert isinstance(defs, dict) and set(defs) == {"quotes", "fact", "criterion"}
    assert schema["required"] == [
        "not_a_job_ad",
        "injection_observed",
        "facts",
        "criteria",
        "summary_en",
        "summary_de",
    ]


def test_the_codes_and_keys_are_the_candidates_enums() -> None:
    c = candidate_c()
    assert get_args(FactItemCode) == tuple(c["$defs"]["fact"]["properties"]["code"]["enum"])
    assert len(get_args(FactItemCode)) == 16
    assert get_args(CriterionKey) == tuple(c["$defs"]["criterion"]["properties"]["criterion"]["enum"])


def test_the_wire_mirror_is_strict() -> None:
    body = generator_wire_body()
    assert GeneratorWire.model_validate(body).facts[0].code == "work_mode"

    def invalid(change: Any) -> dict[str, Any]:
        doc = copy.deepcopy(body)
        change(doc)
        return doc

    for bad in (
        invalid(lambda d: d.update(extra=True)),  # no extra key
        invalid(lambda d: d["facts"][0].update(code="salary")),  # a code outside the enum
        invalid(lambda d: d["criteria"][0].update(level=5)),  # a level outside 0-4
        invalid(lambda d: d["criteria"][0].update(level=True)),  # a boolean is no level
        invalid(lambda d: d["criteria"][0].update(criterion="location_work_mode")),  # not an LLM criterion
        invalid(lambda d: d["facts"][0].update(required="true")),  # strict types
        invalid(lambda d: d["facts"][0].pop("qualifier")),  # every key is required
    ):
        with pytest.raises(ValidationError):
            GeneratorWire.model_validate(bad)
    # the wire form carries any section string: V3 fails an unknown one, the mirror does not
    anything = invalid(
        lambda d: d["criteria"][0]["profile_evidence"].append({"section": "hobbies", "quote": "x"})
    )
    assert GeneratorWire.model_validate(anything).criteria[0].profile_evidence[-1].section == "hobbies"


def test_the_internal_draft_has_v1s_shape_with_any_profile_section() -> None:
    body = generator_draft_body()
    draft = GeneratorDraft.model_validate(body)
    assert draft.job_facts.work_mode.value == "hybrid"
    assert set(type(draft.criteria).model_fields) == set(get_args(CriterionKey))
    body["criteria"]["skills"]["profile_evidence"] = [{"section": "hobbies", "quote": "Python"}]
    assert GeneratorDraft.model_validate(body).criteria.skills.profile_evidence[0].section == "hobbies"
    with pytest.raises(ValidationError):
        GeneratorOut.model_validate(body)  # v1's wire mirror keeps the section enum


def test_v1_hash_is_the_m2_literal_and_v2_keeps_the_m2_templates() -> None:
    t1, s1 = prompt_schema_v1()
    t2, s2 = prompt_schema_v2()
    assert prompt_schema_hash(t1, s1) == _SEEDED_PROMPT_SCHEMA_HASH_V1  # v1 is frozen (Codex r1 #2)
    assert t2["profile_extract"] == t1["profile_extract"] and t2["propose_queries"] == t1["propose_queries"]
    assert s2["critic"] == s1["critic"] and s2["profile"] == s1["profile"] and s2["queries"] == s1["queries"]
    assert prompt_schema_hash(t2, s2) != prompt_schema_hash(t1, s1)
    assert prompt_schema_hash(t2, s2) == _SEEDED_PROMPT_SCHEMA_HASH_V2


def test_the_two_versions_name_their_templates_and_schemas() -> None:
    assert TEMPLATE_NAMES_V1 == ("profile_extract", "propose_queries")
    assert TEMPLATE_NAMES_V2 == (
        "profile_extract",
        "propose_queries",
        "shared_rubric",
        "role_generate",
        "role_critic",
        "role_revise",
    )
    t1, s1 = prompt_schema_v1()
    t2, s2 = prompt_schema_v2()
    assert tuple(t1) == TEMPLATE_NAMES_V1 and tuple(t2) == TEMPLATE_NAMES_V2
    assert s1 == {name: transformed(model) for name, model in SCHEMAS_V1.items()}
    assert s2 == {name: transformed(model) for name, model in SCHEMAS_V2.items()}
    assert s2["generator"] == candidate_c()
    assert RENDERER_VERSION == "1"


def test_the_schema_mappings_are_frozen_constants() -> None:
    assert isinstance(SCHEMAS_V1, MappingProxyType) and isinstance(SCHEMAS_V2, MappingProxyType)
    assert dict(SCHEMAS_V1) == {
        "generator": GeneratorOut,
        "critic": CriticOut,
        "profile": ProfileOut,
        "queries": QueryProposalOut,
    }
    assert dict(SCHEMAS_V2) == {**SCHEMAS_V1, "generator": GeneratorWire}
    with pytest.raises(TypeError):
        SCHEMAS_V1["generator"] = GeneratorWire  # type: ignore[index]


def test_each_parser_family_has_its_mirror() -> None:
    assert PARSER_FAMILIES == ("generator_v1", "generator_v2_items") == get_args(ParserFamily)
    assert mirror_for("generator_v2_items") is GeneratorWire
    assert mirror_for("generator_v1") is GeneratorOut
    with pytest.raises(ValueError, match="parser family"):
        mirror_for("generator_v3")  # type: ignore[arg-type]
