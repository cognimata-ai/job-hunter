"""The `propose_queries` request (spec §10.9, §10.6, §5.5.1): what one try sends for the seeded model policy
and execution config, for a confirmed profile and the scope locations. Offline, no database.

The profile is synthetic (an invented candidate; no name, no contact).
"""

import json
from typing import Any

from jobhunter.config import AppConfig
from jobhunter.db.seed import canonical_json, load_settings_v1
from jobhunter.llm.bounds import PROMPT_OVERHEAD_TOKENS, text_input_bound
from jobhunter.llm.fake import canonical_schema_json
from jobhunter.llm.gateway import node_config
from jobhunter.llm.prompts.render import propose_queries_prompt
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.llm.schemas.queries import QueryProposalOut
from jobhunter.types import get_object

from tests.support.llm import assert_single_call_payload, capture_request

SETTINGS = load_settings_v1()
MODEL_POLICY = get_object(SETTINGS, "scoring.model_policy")
EXECUTION = get_object(SETTINGS, "scoring.execution")
NODE = node_config("propose_queries", model_policy=MODEL_POLICY, execution=EXECUTION, schema=QueryProposalOut)

PROFILE = ConfirmedProfile.model_validate(
    {
        "summary": "Backend-Entwickler für Python-Services in der Logistik.",
        "target_titles": [
            {"title": "Backend Developer", "lang": "en"},
            {"title": "Backend-Entwickler", "lang": "de"},
        ],
        "seniority": "senior",
        "total_years_experience": 7.5,
        "roles": [],
        "skills": [
            {
                "name": "Python",
                "category": "programming_language",
                "level": "expert",
                "years": 7,
                "evidence": "Python",
            }
        ],
        "languages": [
            {"code": "de", "code_other": None, "cefr": "native", "evidence": "Deutsch (Muttersprache)"}
        ],
        "education": [],
        "certifications": [],
        "domains": ["Logistik"],
        "user_entered": {"work_authorization_de": "authorized", "security_clearances": []},
    }
)
LOCATIONS = ["Hamburg", "Lüneburg"]


def _query(i: int, *, exploration: bool = False) -> dict[str, Any]:
    return {
        "text": f"Backend Entwickler Python {i}",
        "lang": "de",
        "target_title": "Backend-Entwickler",
        "exploration": exploration,
        "rationale": "Kernrolle des Profils.",
    }


ANSWER: dict[str, Any] = {"queries": [_query(i, exploration=i == 5) for i in range(6)]}


def test_the_node_is_the_seeded_propose_queries_policy() -> None:
    """The defaults of §10.3: Opus at medium effort, 8,000 tokens first."""
    assert (NODE.model, NODE.effort, NODE.max_tokens, NODE.max_tokens_retry) == (
        "claude-opus-5-5",
        "medium",
        8_000,
        16_000,
    )


async def test_the_query_proposal_request_follows_the_policy(payload_cfg: AppConfig) -> None:
    prompt = propose_queries_prompt(PROFILE, LOCATIONS)
    request = await capture_request(payload_cfg, NODE, prompt, ANSWER)
    assert_single_call_payload(request, NODE, model_policy=MODEL_POLICY, execution=EXECUTION)
    body = request.body
    assert body["system"] == prompt.system[0].text
    (message,) = body["messages"]
    assert message["role"] == "user"
    content = message["content"]
    # The confirmed profile as canonical JSON without the user-entered block, then the locations.
    profile_json = canonical_json(PROFILE.model_dump(mode="json", exclude={"user_entered"})).decode("utf-8")
    assert f"<candidate_profile>{profile_json}</candidate_profile>" in content
    assert f"<search_locations>{json.dumps(LOCATIONS, ensure_ascii=False)}</search_locations>" in content
    assert "work_authorization_de" not in content and "authorized" not in content
    assert request.headers["x-jh-purpose"] == "propose_queries"


async def test_the_input_bound_counts_exactly_the_sent_components(payload_cfg: AppConfig) -> None:
    prompt = propose_queries_prompt(PROFILE, LOCATIONS)
    body = (await capture_request(payload_cfg, NODE, prompt, ANSWER)).body
    rendered = prompt.as_request(NODE.schema)
    assert rendered.system == (body["system"],)
    assert rendered.messages == tuple(m["content"] for m in body["messages"])
    assert rendered.schema_json == canonical_schema_json(body["output_config"]["format"]["schema"])
    sent_bytes = sum(
        len(t.encode("utf-8")) for t in (*rendered.system, *rendered.messages, rendered.schema_json)
    )
    assert text_input_bound(NODE.model, rendered) == sent_bytes + PROMPT_OVERHEAD_TOKENS[NODE.model]
