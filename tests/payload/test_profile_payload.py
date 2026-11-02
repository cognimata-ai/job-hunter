"""The `extract_profile` request (spec §10.9, §10.6): what one try sends for the seeded model policy and
execution config, for a reviewed CV text and for a native PDF. Offline, no database.

The CV is synthetic: an invented candidate (the contact stripping left `[NAME]` and the other placeholders)
and an invented employer.
"""

import base64
from typing import Any

from jobhunter.config import AppConfig
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm.bounds import PROMPT_OVERHEAD_TOKENS, text_input_bound
from jobhunter.llm.fake import canonical_schema_json
from jobhunter.llm.gateway import WireSchema, node_config
from jobhunter.llm.prompts.render import (
    extract_profile_native_pdf_prompt,
    extract_profile_prompt,
    template_version,
)
from jobhunter.llm.schemas.profile import ProfileOut
from jobhunter.types import get_object

from tests.support.llm import assert_single_call_payload, capture_request

SETTINGS = load_settings_v1()
MODEL_POLICY = get_object(SETTINGS, "scoring.model_policy")
EXECUTION = get_object(SETTINGS, "scoring.execution")
NODE = node_config(
    "extract_profile",
    model_policy=MODEL_POLICY,
    execution=EXECUTION,
    schema=WireSchema.from_model(ProfileOut),
)

# German typography on purpose: the byte bound counts UTF-8 bytes, not characters.
REVIEWED = (
    "[NAME]\n[ADDRESS] · [PHONE] · [EMAIL]\n\n"
    "Berufserfahrung\n"
    "03/2019 \u2013 heute: Senior Backend-Entwickler, Beispiel Logistik GmbH\n"
    "\u2022 Tourenplanung mit Python, FastAPI und PostgreSQL neu gebaut (\u201eGroße Umstellung\u201c)\n"
    "Sprachen\nDeutsch (Muttersprache), Englisch (verhandlungssicher)\n"
)
ANSWER: dict[str, Any] = {
    "summary": "Backend-Entwickler für Python-Services in der Logistik.",
    "target_titles": [
        {"title": "Backend Developer", "lang": "en"},
        {"title": "Backend-Entwickler", "lang": "de"},
    ],
    "seniority": "senior",
    "total_years_experience": None,
    "roles": [
        {
            "title": "Senior Backend-Entwickler",
            "employer": "Beispiel Logistik GmbH",
            "start": "2019-03-01",
            "end": "present",
            "seniority": "senior",
            "domain": "Logistik",
            "highlights": ["Tourenplanung mit Python, FastAPI und PostgreSQL neu gebaut"],
        }
    ],
    "skills": [
        {
            "name": "Python",
            "category": "programming_language",
            "level": "advanced",
            "years": None,
            "evidence": "Tourenplanung mit Python",
        }
    ],
    "languages": [
        {"code": "de", "code_other": None, "cefr": "native", "evidence": "Deutsch (Muttersprache)"},
        {"code": "en", "code_other": None, "cefr": "C1", "evidence": "Englisch (verhandlungssicher)"},
    ],
    "education": [],
    "certifications": [],
    "domains": ["Logistik"],
}


def test_the_node_is_the_seeded_extract_profile_policy() -> None:
    """The defaults of §10.3: Opus at medium effort, 16,000 tokens first."""
    assert (NODE.model, NODE.effort, NODE.max_tokens, NODE.max_tokens_retry) == (
        "claude-opus-5-5",
        "medium",
        16_000,
        32_000,
    )


async def test_the_text_extraction_request_follows_the_policy(payload_cfg: AppConfig) -> None:
    prompt = extract_profile_prompt(REVIEWED)
    request = await capture_request(payload_cfg, NODE, prompt, ANSWER)
    assert_single_call_payload(request, NODE, model_policy=MODEL_POLICY, execution=EXECUTION)
    body = request.body
    # One static system block (sent as a plain string: no cache marker), the reviewed text in the user turn.
    assert body["system"] == prompt.system[0].text
    (message,) = body["messages"]
    assert message == {"role": "user", "content": f"<cv_text>{REVIEWED}</cv_text>"}
    assert request.headers["x-jh-purpose"] == "extract_profile"
    assert prompt.template_version == template_version("profile_extract")


async def test_the_input_bound_counts_exactly_the_sent_components(payload_cfg: AppConfig) -> None:
    """The byte bound (§10.7) is taken over what the request carries: its system text, its user turn and the
    sent schema, in UTF-8 bytes, plus the overhead."""
    prompt = extract_profile_prompt(REVIEWED)
    body = (await capture_request(payload_cfg, NODE, prompt, ANSWER)).body
    rendered = prompt.as_request(NODE.schema)
    assert rendered.system == (body["system"],)
    assert rendered.messages == tuple(m["content"] for m in body["messages"])
    assert rendered.schema_json == canonical_schema_json(body["output_config"]["format"]["schema"])
    sent_bytes = sum(
        len(text.encode("utf-8"))
        for text in (body["system"], body["messages"][0]["content"], rendered.schema_json)
    )
    assert sent_bytes > len(body["system"]) + len(body["messages"][0]["content"]) + len(rendered.schema_json)
    assert text_input_bound(NODE.model, rendered) == sent_bytes + PROMPT_OVERHEAD_TOKENS[NODE.model]


async def test_the_native_pdf_request_sends_a_document_block_and_follows_the_policy(
    payload_cfg: AppConfig,
) -> None:
    pdf = b"%PDF-1.7\n% a synthetic one-page CV of an invented candidate\n%%EOF\n"
    prompt = extract_profile_native_pdf_prompt(pdf)
    request = await capture_request(payload_cfg, NODE, prompt, ANSWER)
    assert_single_call_payload(request, NODE, model_policy=MODEL_POLICY, execution=EXECUTION)
    body = request.body
    assert body["system"] == prompt.system[0].text == extract_profile_prompt(REVIEWED).system[0].text
    (message,) = body["messages"]
    document, instruction = message["content"]
    assert document == {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf).decode()},
    }
    assert instruction["type"] == "text" and "cv_text" not in instruction["text"]
