"""The schema dry-run of a loaded prompt-schema version (spec §10.11; M3 Task 4), offline: the cases a version
and a model policy give, each case's node (the version's stored wire schema and its structural mirror), the
keyword probe on its probe-only schema, and the synthetic v2 streams the fake replays until Task 26 records
live ones.
"""

import copy
from pathlib import Path
from typing import Any, Final, cast

import pytest
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import fake, schema_dry_run
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import LLMGateway, _chat, to_messages
from jobhunter.llm.pricing import Model
from jobhunter.llm.schema_dry_run import PROBE_ONLY_SCHEMA, DryRunCase, node_for, probe_schema
from jobhunter.llm.schemas import transformed
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.generator import GeneratorOut, GeneratorWire
from jobhunter.llm.schemas.profile import ProfileOut
from jobhunter.llm.schemas.queries import QueryProposalOut
from jobhunter.llm.transport import make_http_client
from jobhunter.types import JSONValue, get_object
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import (
    PAYLOAD_KEY,
    fake_llm_config,
    synthetic_critic_sse,
    synthetic_generator_sse,
    v1_version,
    v2_version,
)

SONNET: Final[Model] = "claude-sonnet-5-5"
OPUS: Final[Model] = "claude-opus-5-5"
HAIKU: Final[Model] = "claude-haiku-5-5"
SEEDED_POLICY = get_object(load_settings_v1(), "scoring.model_policy")
V2 = v2_version(2)
V1 = v1_version(1)


def policy(**models: str) -> dict[str, JSONValue]:
    doc = copy.deepcopy(dict(SEEDED_POLICY))
    nodes = get_object(doc, "nodes")
    for name, model in models.items():
        nodes[name] = {**get_object(nodes, name), "model": model}
    return doc


def test_the_cases_of_version_2_on_the_seeded_policy() -> None:
    assert schema_dry_run.cases(SEEDED_POLICY) == [
        DryRunCase("generator", SONNET),
        DryRunCase("critic", OPUS),
        DryRunCase("profile", OPUS),
        DryRunCase("queries", OPUS),
        DryRunCase("generator", SONNET, probe=True),
    ]


def test_a_reviser_on_another_model_is_a_second_generator_case() -> None:
    assert schema_dry_run.cases(policy(revise=HAIKU)) == [
        DryRunCase("generator", SONNET),
        DryRunCase("generator", HAIKU),
        DryRunCase("critic", OPUS),
        DryRunCase("profile", OPUS),
        DryRunCase("queries", OPUS),
        DryRunCase("generator", SONNET, probe=True),
    ]


def test_each_case_sends_the_versions_stored_schema_and_parses_with_its_mirror() -> None:
    mirrors = {
        "generator": GeneratorWire,
        "critic": CriticOut,
        "profile": ProfileOut,
        "queries": QueryProposalOut,
    }
    for case in schema_dry_run.cases(SEEDED_POLICY):
        node = node_for(case, V2)
        assert node.schema.json == V2.schemas[case.schema_name]
        assert node.schema.model is mirrors[case.schema_name]
        assert (node.purpose, node.effort, node.max_tokens) == ("schema_dry_run", "low", 2_000)
    generator = node_for(DryRunCase("generator", SONNET), V2)
    assert generator.schema.json == transformed(GeneratorWire)  # candidate C
    # version 1's generator case keeps its own schema and mirror
    v1 = node_for(DryRunCase("generator", SONNET), V1)
    assert v1.schema.json == transformed(GeneratorOut) and v1.schema.model is GeneratorOut


def test_the_probe_of_a_schema_without_a_plain_integer_is_the_probe_only_schema() -> None:
    assert PROBE_ONLY_SCHEMA == {
        "type": "object",
        "additionalProperties": False,
        "required": ["n"],
        "properties": {"n": {"type": "integer", "minimum": 0}},
    }
    c = V2.schemas["generator"]
    assert schema_dry_run._first_plain_integer(copy.deepcopy(c)) is None  # C's one integer is the level enum
    probe = probe_schema(cast(dict[str, Any], c))
    assert probe == PROBE_ONLY_SCHEMA
    probe["properties"]["n"].pop("minimum")
    assert probe_schema(cast(dict[str, Any], c)) == PROBE_ONLY_SCHEMA  # a new copy each call
    # version 1's generator keeps M2's probe: its first plain integer gains the keyword
    v1 = probe_schema(cast(dict[str, Any], V1.schemas["generator"]))
    assert v1["$defs"]["nullable_int"]["anyOf"][0] == {"type": "integer", "minimum": 0}


async def test_the_v2_probe_body_is_built_offline_and_sends_the_keyword(tmp_path: Path) -> None:
    cfg = fake_llm_config(tmp_path / "anthropic", tmp_path / "no-secrets")
    async with make_http_client(cfg) as client:
        # a count body touches no database and sends nothing
        gateway = LLMGateway(cast(AsyncEngine, None), cfg, http_client=client, cache=CacheMonitor())
        case = DryRunCase("generator", SONNET, probe=True)
        body = schema_dry_run.probe_count_body(gateway, case, V2)
        plain = gateway.count_body(
            node_for(DryRunCase("generator", SONNET), V2), schema_dry_run.prompt_for(case)
        )
    assert body["model"] == SONNET
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": PROBE_ONLY_SCHEMA}
    assert body["output_config"]["format"]["schema"]["properties"]["n"]["minimum"] == 0
    assert "max_tokens" not in body and "stream" not in body
    # the rest is the generator case's own count body, whose schema is candidate C
    assert plain["output_config"]["format"]["schema"] == V2.schemas["generator"]
    assert {k: v for k, v in body.items() if k != "output_config"} == {
        k: v for k, v in plain.items() if k != "output_config"
    }
    assert body["output_config"]["effort"] == plain["output_config"]["effort"] == "low"


@pytest.mark.parametrize("schema_name", ["generator", "critic"])
async def test_the_fake_replays_a_synthetic_v2_stream_through_the_gateways_parse(
    tmp_path: Path, schema_name: str
) -> None:
    """Until Task 26 records the live v2 answers, the synthetic C-shaped and critic-shaped streams stand in:
    the fake serves them for the stored schema, and they parse as the mirror and conform to the schema."""
    case = DryRunCase(schema_name, SONNET if schema_name == "generator" else OPUS)  # type: ignore[arg-type]
    node = node_for(case, V2)
    root = tmp_path / "anthropic"
    stream = (synthetic_generator_sse if schema_name == "generator" else synthetic_critic_sse)(
        model=case.model
    )
    path = fake.recording_path(root, "schema_dry_run", case.model, node.schema.json)
    path.parent.mkdir(parents=True)
    path.write_bytes(stream)
    cfg = fake_llm_config(root, tmp_path / "no-secrets")
    async with make_http_client(cfg) as client:
        runnable = _chat(
            node.model,
            node.effort,
            node.max_tokens,
            node.schema.json,
            api_key=PAYLOAD_KEY,
            http_client=client,
            purpose=node.purpose,
        )
        out = await runnable.ainvoke(to_messages(schema_dry_run.prompt_for(case)))
    assert out["parsing_error"] is None
    parsed = node.schema.model.model_validate(out["parsed"])
    assert schema_dry_run.schema_violations(node.schema.json, parsed.model_dump(mode="json")) == []
