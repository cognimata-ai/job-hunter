"""Stored prompt-schema versions render what they store (M3 Task 4; Codex r1 #3, r2 B3, r4 B3; plan Deviation
40): the database row is the authority after the seed, and its parser family is an explicit stored constant.

- Two stored versions with different role templates and generator schemas send their own bytes, and each
  call row names the version that rendered it.
- The seed writes `generator_v2_items` on the shipped M3 version and `generator_v1` on M2's; nothing derives
  the family from the schema, and a `generator_v1` version cannot score.
- `schemas.PARSER_FAMILIES` is the member list of the column's CHECK.
- The stored schemas keep their key order through the database (`schemas` is `json`, Ruling T4-a), so what a
  score render and the dry-run send is candidate C in the order V4b sent it; a stored schema the SDK's
  transform would change is refused at load, and a load hands out its own copy.
"""

import copy
import hashlib
import importlib.util
import json
import re
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Final, cast

import anthropic
import pytest
import sqlalchemy as sa
from jobhunter.db.seed import load_settings_v1
from jobhunter.db.sql import CHECK_VIOLATION
from jobhunter.llm import schema_dry_run
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import CallContext, LLMGateway, NodeConfig, node_config
from jobhunter.llm.prompts.render import (
    PromptVersion,
    PromptVersionUnscorable,
    ScoreRole,
    job_ad_block,
    load_prompt_version,
    prompt_schema_hash,
    prompt_schema_v1,
    prompt_schema_v2,
    score_prompt,
    score_schema,
)
from jobhunter.llm.schemas import PARSER_FAMILIES, mirror_for, transformed
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.spend_ledger import EvalRunOwner
from jobhunter.llm.transport import make_http_client
from jobhunter.scoring.rubric import RUBRIC_V2, load_rubric
from jobhunter.types import get_object
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import (
    CapturingTransport,
    active_execution_config,
    capture_request,
    critic_body,
    fake_llm_config,
    generator_wire_body,
    score_profile,
    synthetic_sse,
)
from tests.support.m2 import insert_eval_run
from tests.support.schema import refused

pytestmark = pytest.mark.integration
SONNET: Final = "claude-sonnet-5-5"
_INSERT = sa.text(
    "INSERT INTO prompt_schema_versions (prompt_schema_hash, templates, schemas, parser_family) "
    "VALUES (:hash, CAST(:templates AS jsonb), CAST(:schemas AS json), :family) RETURNING id"
)
_FAMILY_OF = sa.text("SELECT parser_family FROM prompt_schema_versions WHERE prompt_schema_hash = :hash")
_FAMILY_CHECK = sa.text(
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'prompt_schema_versions'::regclass "
    "AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%parser_family%'"
)
_CALLS = sa.text("SELECT prompt_template_version, input_refs FROM llm_calls ORDER BY id")
AD = job_ad_block(
    title="Backend Engineer (Python)",
    company="Beispiel Logistik GmbH",
    locations=["Hamburg"],
    work_modes=[("hybrid", "structured")],
    employment_types=[("full_time", "structured")],
    completeness="full",
    description_text="Du entwickelst Services in Python und PostgreSQL für die Sendungsverfolgung.",
)


async def insert_version(
    engine: AsyncEngine, templates: dict[str, Any], schemas: dict[str, Any], *, family: str
) -> int:
    """A version row as a test writes one: its own content hash, the family it names."""
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    _INSERT,
                    {
                        "hash": prompt_schema_hash(templates, schemas),
                        "templates": json.dumps(templates),
                        "schemas": json.dumps(schemas),
                        "family": family,
                    },
                )
            ).scalar_one()
        )


async def load(engine: AsyncEngine, version_id: int) -> PromptVersion:
    async with engine.connect() as conn:
        return await load_prompt_version(conn, version_id)


def v2_content() -> tuple[dict[str, Any], dict[str, Any]]:
    """Version 2's templates and schemas as plain JSON documents (copies)."""
    templates, schemas = prompt_schema_v2()
    return dict(templates), copy.deepcopy(dict(schemas))


def variant(marker: str, *, reversed_basis: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """Version 2's content with its own generate role text and a generator schema that differs in a
    `description` string and, for one of the two, in the order of an enum."""
    templates, schemas = v2_content()
    templates["role_generate"] = f"{templates['role_generate']}\n\nVariant {marker}."
    generator = schemas["generator"]
    generator["properties"]["summary_en"]["description"] = f"The English summary ({marker})."
    if reversed_basis:
        basis = generator["$defs"]["criterion"]["properties"]["basis"]
        basis["enum"] = list(reversed(basis["enum"]))
    return templates, schemas


async def test_two_stored_versions_render_their_own_content(engine: AsyncEngine, tmp_path: Path) -> None:
    a_id = await insert_version(engine, *variant("A", reversed_basis=False), family="generator_v2_items")
    b_id = await insert_version(engine, *variant("B", reversed_basis=True), family="generator_v2_items")
    versions = [await load(engine, a_id), await load(engine, b_id)]
    assert versions[0].schemas["generator"] != versions[1].schemas["generator"]
    # the capturing transport answers every try with one C-shaped body, which both schemas allow
    transport = CapturingTransport(synthetic_sse(generator_wire_body(), model=SONNET))
    cfg = fake_llm_config(tmp_path / "unused", tmp_path / "no-secrets")
    execution_config_id = await active_execution_config(engine)
    async with engine.begin() as conn:
        eval_run_id = await insert_eval_run(conn)
    async with make_http_client(cfg, mount=transport) as client:
        gateway = LLMGateway(engine, cfg, http_client=client, cache=CacheMonitor())
        results = []
        for version in versions:
            prompt = score_prompt(
                "generate", version=version, rubric=load_rubric(RUBRIC_V2), profile=score_profile(), ad=AD
            )
            node = NodeConfig(
                purpose="generate",
                model=SONNET,
                effort="medium",
                max_tokens=6_000,
                max_tokens_retry=12_000,
                deadline_s=600,
                schema=score_schema("generate", version),
            )
            ctx = CallContext(
                owner=EvalRunOwner(eval_run_id),
                purpose_class="nonscoring",
                execution_config_id=execution_config_id,
                deadline=time.monotonic() + 600,
                eval_run_id=eval_run_id,
            )
            results.append(await gateway.call(node, prompt, ctx))
    assert len(transport.requests) == 2
    for version, request, result in zip(versions, transport.requests, results, strict=True):
        body = request.body
        assert body["system"][2]["text"] == version.templates["role_generate"]  # its own role text
        assert body["system"][2]["text"].endswith(f"Variant {'A' if version.id == a_id else 'B'}.")
        assert body["output_config"]["format"]["schema"] == version.schemas["generator"]  # its own bytes
        # the parsed output validates against the mirror of the version's stored family
        mirror = mirror_for(version.parser_family)
        assert isinstance(result.parsed, mirror)
        assert mirror.model_validate(result.parsed.model_dump(mode="json")) == result.parsed
    async with engine.connect() as conn:
        calls = (await conn.execute(_CALLS)).all()
    assert [(c.prompt_template_version, c.input_refs["prompt_schema_version_id"]) for c in calls] == [
        (f"generate@{a_id}", a_id),
        (f"generate@{b_id}", b_id),
    ]
    assert all(c.input_refs["truncated"] is False for c in calls)


async def test_parser_family_is_the_stored_constant(engine: AsyncEngine) -> None:
    v1_hash, v2_hash = prompt_schema_hash(*prompt_schema_v1()), prompt_schema_hash(*prompt_schema_v2())
    async with engine.connect() as conn:
        # the seed (`migrated`) wrote both shipped rows, each with its family, as literals here
        assert (await conn.execute(_FAMILY_OF, {"hash": v2_hash})).scalar_one() == "generator_v2_items"
        assert (await conn.execute(_FAMILY_OF, {"hash": v1_hash})).scalar_one() == "generator_v1"
        ids = {
            row.prompt_schema_hash: int(row.id)
            for row in await conn.execute(
                sa.text("SELECT id, prompt_schema_hash FROM prompt_schema_versions")
            )
        }
    shipped_v2, shipped_v1 = await load(engine, ids[v2_hash]), await load(engine, ids[v1_hash])
    assert (shipped_v2.parser_family, shipped_v1.parser_family) == ("generator_v2_items", "generator_v1")
    assert (shipped_v2.prompt_schema_hash, shipped_v1.prompt_schema_hash) == (v2_hash, v1_hash)
    templates, schemas = v2_content()
    assert dict(shipped_v2.templates) == templates and dict(shipped_v2.schemas) == schemas
    # an inactive version with v2's templates, named generator_v1 by its writer: the stored name decides
    templates["role_generate"] += "\n"
    named_v1 = await load(engine, await insert_version(engine, templates, schemas, family="generator_v1"))
    assert named_v1.parser_family == "generator_v1"
    with pytest.raises(PromptVersionUnscorable):
        score_prompt(
            "generate", version=named_v1, rubric=load_rubric(RUBRIC_V2), profile=score_profile(), ad=AD
        )
    score_prompt(
        "generate", version=shipped_v2, rubric=load_rubric(RUBRIC_V2), profile=score_profile(), ad=AD
    )
    # any other family is refused by the CHECK
    async with engine.connect() as conn:
        templates["role_generate"] += "\n"
        info = await refused(
            conn,
            "INSERT INTO prompt_schema_versions (prompt_schema_hash, templates, schemas, parser_family) "
            "VALUES (:hash, '{}', '{}', 'generator_v3')",
            {"hash": hashlib.sha256(b"another version").digest()},
        )
    assert info.sqlstate == CHECK_VIOLATION


async def test_a_missing_version_is_a_lookup_error(engine: AsyncEngine) -> None:
    with pytest.raises(LookupError, match="prompt-schema version"):
        await load(engine, 987_654_321)


async def test_parser_families_pin_the_check(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        definition = (await conn.execute(_FAMILY_CHECK)).scalar_one()
    assert tuple(sorted(re.findall(r"'([^']*)'::text", definition))) == tuple(sorted(PARSER_FAMILIES))
    assert len(PARSER_FAMILIES) == len(set(PARSER_FAMILIES)) == 2


# ---- the key order through the database (Ruling T4-a) ----------------------------------------------------

REPO: Final = Path(__file__).resolve().parents[2]
_ID_OF: Final = sa.text("SELECT id FROM prompt_schema_versions WHERE prompt_schema_hash = :hash")


def _load_spike(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts/spikes" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


candidate_c = _load_spike("generator_v2_candidates").candidate_c


def key_order(value: Any) -> Any:
    """Every object's keys in their order, at every level (lists keep theirs)."""
    if isinstance(value, dict):
        return [(key, key_order(item)) for key, item in value.items()]
    if isinstance(value, list):
        return [key_order(item) for item in value]
    return None


def property_orders(value: Any, path: str = "$") -> dict[str, list[str]]:
    """The key order of every `properties` and `$defs` object: the order a structured output follows."""
    found: dict[str, list[str]] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("properties", "$defs") and isinstance(item, dict):
                found[f"{path}.{key}"] = list(item)
            found.update(property_orders(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found.update(property_orders(item, f"{path}[{i}]"))
    return found


async def seeded(engine: AsyncEngine, content: tuple[dict[str, Any], dict[str, Any]]) -> PromptVersion:
    async with engine.connect() as conn:
        version_id = (await conn.execute(_ID_OF, {"hash": prompt_schema_hash(*content)})).scalar_one()
    return await load(engine, int(version_id))


async def test_the_stored_schemas_keep_their_key_order_through_the_database(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """After seed and load, the generator schema a score render and the dry-run send is candidate C with its
    properties and `$defs` in the order A3 printed them (the flags first, a criterion's level before its
    evidence), and every key where the V4b probe sent it: the SDK's transform, which every request applies,
    orders the keywords of a node and keeps the order of its properties. The critic's is the code's, and
    version 1's are `prompt_schema_v1()`'s."""
    v2 = await seeded(engine, prompt_schema_v2())
    probed = anthropic.transform_schema(copy.deepcopy(candidate_c()))  # what V4b sent
    _, code_v2 = prompt_schema_v2()
    assert key_order(v2.schemas["generator"]) == key_order(code_v2["generator"]) == key_order(probed)
    assert property_orders(v2.schemas["generator"]) == property_orders(candidate_c())
    assert list(cast(dict[str, Any], v2.schemas["generator"])["properties"]) == [
        "not_a_job_ad",
        "injection_observed",
        "facts",
        "criteria",
        "summary_en",
        "summary_de",
    ]
    for name in ("critic", "profile", "queries"):
        assert key_order(v2.schemas[name]) == key_order(code_v2[name]), name
    assert key_order(v2.schemas["critic"]) == key_order(transformed(CriticOut))
    # what the score nodes and the dry-run hand the gateway
    assert key_order(score_schema("generate", v2).json) == key_order(probed)
    assert key_order(score_schema("critic", v2).json) == key_order(transformed(CriticOut))
    policy = get_object(load_settings_v1(), "scoring.model_policy")
    execution = get_object(load_settings_v1(), "scoring.execution")
    for case in schema_dry_run.cases(policy):
        if not case.probe:
            sent = schema_dry_run.node_for(case, v2).schema.json
            assert key_order(sent) == key_order(code_v2[case.schema_name]), case.label
    # and what reaches the wire, as the request body serialises it (the schema is the node's; the prompt is
    # the generator's for both, which the schema does not depend on)
    cfg = fake_llm_config(tmp_path / "unused", tmp_path / "no-secrets")
    roles: tuple[tuple[ScoreRole, dict[str, Any], Any], ...] = (
        ("generate", generator_wire_body(), probed),
        ("critic", critic_body(), transformed(CriticOut)),
    )
    for role, answer, expected in roles:
        node = node_config(role, model_policy=policy, execution=execution, schema=score_schema(role, v2))
        prompt = score_prompt(
            "generate",
            version=v2,
            rubric=load_rubric(RUBRIC_V2),
            profile=score_profile(),
            ad=AD,
        )
        request = await capture_request(cfg, node, prompt, answer)
        wire = request.body["output_config"]["format"]["schema"]
        assert json.dumps(wire) == json.dumps(expected), role
    # version 1, as the M2 code gives it
    v1 = await seeded(engine, prompt_schema_v1())
    _, code_v1 = prompt_schema_v1()
    assert {name: key_order(schema) for name, schema in v1.schemas.items()} == {
        name: key_order(schema) for name, schema in code_v1.items()
    }


async def test_a_stored_schema_the_sdk_would_change_is_refused_at_load(engine: AsyncEngine) -> None:
    """Every reader of a stored schema (`output_schema`, `count_body`, the byte bound) reads what the request
    sends only when the SDK's transform leaves it unchanged; a row that it would change is refused."""
    templates, schemas = v2_content()
    schemas["critic"]["properties"]["count"] = {"type": "integer", "minimum": 0}  # moved into a description
    with pytest.raises(ValueError, match="'critic' schema"):
        await load(engine, await insert_version(engine, templates, schemas, family="generator_v2_items"))
    templates, schemas = v2_content()
    schemas["profile"] = ["no", "schema"]
    with pytest.raises(ValueError, match="'profile' schema"):
        await load(engine, await insert_version(engine, templates, schemas, family="generator_v2_items"))


async def test_a_loaded_version_is_the_callers_own_copy(engine: AsyncEngine) -> None:
    """The cache is never shared out: what one caller does to its version or a render's schema never reaches
    a later load or render."""
    _, code_v2 = prompt_schema_v2()
    first = await seeded(engine, prompt_schema_v2())
    cast(dict[str, Any], first.schemas["generator"])["properties"].clear()
    cast(dict[str, Any], score_schema("critic", first).json)["$defs"].clear()
    again = await seeded(engine, prompt_schema_v2())
    assert dict(again.schemas) == code_v2 and again is not first
    assert score_schema("generate", again).json == code_v2["generator"]
