"""The score requests (spec §10.9, §10.6; M3 Task 4): what one try of each score role sends for the seeded
model policy and execution config, rendered by `render.score_prompt` from prompt-schema version 2. Offline, no
database: one `gateway._chat` try on the capturing transport.

The profile, the ad and the draft are synthetic (an invented candidate and employer).
"""

from typing import Any, Final, Literal

import pytest
from jobhunter.config import AppConfig
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm.bounds import PROMPT_OVERHEAD_TOKENS, text_input_bound
from jobhunter.llm.fake import canonical_schema_json
from jobhunter.llm.gateway import NodeConfig, RenderedPrompt, node_config
from jobhunter.llm.prompts.render import (
    Issue,
    draft_block,
    issues_block,
    job_ad_block,
    required_facts_block,
    score_prompt,
    score_schema,
)
from jobhunter.llm.schemas.generator import GeneratorDraft
from jobhunter.scoring.facts import derive_fact_ids
from jobhunter.scoring.rubric import RUBRIC_V2, load_rubric
from jobhunter.types import get_object

from tests.support.llm import (
    BANNED_REQUEST_KEYS,
    cache_control_paths,
    capture_request,
    critic_body,
    generator_draft_body,
    generator_wire_body,
    score_profile,
    v2_version,
)

Role = Literal["generate", "critic", "revise"]
ROLES: Final[tuple[Role, ...]] = ("generate", "critic", "revise")
SETTINGS = load_settings_v1()
MODEL_POLICY = get_object(SETTINGS, "scoring.model_policy")
EXECUTION = get_object(SETTINGS, "scoring.execution")
VERSION = v2_version(2)
DRAFT = GeneratorDraft.model_validate(generator_draft_body())
AD = job_ad_block(
    title="Backend Engineer (Python)",
    company="Beispiel Logistik GmbH",
    locations=["Hamburg"],
    work_modes=[("hybrid", "structured")],
    employment_types=[("full_time", "structured")],
    completeness="full",
    description_text=(
        "Wir suchen eine:n Backend Engineer für die Sendungsverfolgung. Du entwickelst Services in Python "
        "und PostgreSQL; sehr gute Deutschkenntnisse sind Voraussetzung, gute Englischkenntnisse (C1) ein "
        "Plus."
    ),
)


def node(role: Role) -> NodeConfig:
    return node_config(
        role, model_policy=MODEL_POLICY, execution=EXECUTION, schema=score_schema(role, VERSION)
    )


def prompt(role: Role) -> RenderedPrompt:
    common: dict[str, Any] = {
        "version": VERSION,
        "rubric": load_rubric(RUBRIC_V2),
        "profile": score_profile(),
    }
    facts = derive_fact_ids(DRAFT.job_facts)
    issues = issues_block(
        [Issue("level_too_high", "languages", ("gute Englischkenntnisse",), suggested_level=2)]
    )
    if role == "critic":
        return score_prompt(
            role,
            **common,
            ad=AD,
            draft=draft_block(DRAFT, facts),
            required_facts=required_facts_block(list(facts.values())),
            validator_issues=issues,
        )
    if role == "revise":
        return score_prompt(role, **common, ad=AD, previous_draft=draft_block(DRAFT, facts), issues=issues)
    return score_prompt(role, **common, ad=AD)


def answer(role: Role) -> dict[str, Any]:
    return critic_body() if role == "critic" else generator_wire_body()


def test_the_nodes_are_the_seeded_score_policy() -> None:
    """§10.3 policy B: Sonnet generates and revises, Opus criticises, each at medium, 6,000 tokens first."""
    assert [(node(r).model, node(r).effort, node(r).max_tokens, node(r).max_tokens_retry) for r in ROLES] == [
        ("claude-sonnet-5-5", "medium", 6_000, 12_000),
        ("claude-opus-5-5", "medium", 6_000, 12_000),
        ("claude-sonnet-5-5", "medium", 6_000, 12_000),
    ]


@pytest.mark.parametrize("role", ROLES)
async def test_a_score_request_has_the_three_system_blocks_and_one_user_turn(
    payload_cfg: AppConfig, role: Role
) -> None:
    rendered = prompt(role)
    request = await capture_request(payload_cfg, node(role), rendered, answer(role))
    body = request.body
    assert (request.url.host, request.url.path) == ("api.anthropic.com", "/v1/messages")
    # system[0] the shared rubric, system[1] the profile with the one breakpoint, system[2] the role
    assert body["system"] == [
        {"type": "text", "text": rendered.system[0].text},
        {"type": "text", "text": rendered.system[1].text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": rendered.system[2].text},
    ]
    assert cache_control_paths(body) == ["/system/1/cache_control"]
    # one user turn, never an assistant turn (L3878)
    assert body["messages"] == [{"role": "user", "content": rendered.messages[0].content}]
    # the role's schema as the version stores it, the policy's effort, the first stage, streamed
    expected = VERSION.schemas["critic" if role == "critic" else "generator"]
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": expected}
    assert body["output_config"]["effort"] == get_object(MODEL_POLICY, f"nodes.{role}")["effort"]
    assert body["max_tokens"] == get_object(EXECUTION, role)["max_tokens"]
    assert body["stream"] is True
    assert not set(BANNED_REQUEST_KEYS) & set(body), sorted(set(BANNED_REQUEST_KEYS) & set(body))
    assert "budget_tokens" not in (body.get("thinking") or {})
    assert request.headers["x-jh-purpose"] == role
    assert "anthropic-beta" not in request.headers


@pytest.mark.parametrize("role", ROLES)
async def test_the_input_bound_covers_every_component_and_the_schema(
    payload_cfg: AppConfig, role: Role
) -> None:
    rendered = prompt(role)
    body = (await capture_request(payload_cfg, node(role), rendered, answer(role))).body
    request = rendered.as_request(node(role).schema)
    assert request.system == tuple(block["text"] for block in body["system"])
    assert request.messages == tuple(m["content"] for m in body["messages"])
    assert request.schema_json == canonical_schema_json(body["output_config"]["format"]["schema"])
    sent = sum(
        len(text.encode("utf-8")) for text in (*request.system, *request.messages, request.schema_json)
    )
    assert text_input_bound(node(role).model, request) == sent + PROMPT_OVERHEAD_TOKENS[node(role).model]
