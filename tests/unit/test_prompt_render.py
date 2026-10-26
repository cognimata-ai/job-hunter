"""The M2 prompt templates and their renderer (M2 Task 6): the layout of each rendered request (§10.6: one
static system block, no cache marker, the input in the user turn), the template versions, and prompt-schema
version 1 with its hash (§7.3).

Every input here is synthetic (invented people and employers only).
"""

import base64
import hashlib
import json
import re
from importlib import resources

import pytest
from jobhunter.db.seed import canonical_json
from jobhunter.llm.gateway import RenderedPrompt
from jobhunter.llm.prompts import render
from jobhunter.llm.prompts.render import (
    RENDERER_VERSION,
    extract_profile_native_pdf_prompt,
    extract_profile_prompt,
    prompt_schema_hash,
    prompt_schema_v1,
    propose_queries_prompt,
    template_version,
)
from jobhunter.llm.schemas import SCHEMAS_V1, transformed
from jobhunter.llm.schemas.profile import ConfirmedProfile
from langchain_core.messages import HumanMessage

# A reviewed CV text as the contact stripping leaves it (§12.3): an invented candidate and employer.
REVIEWED = (
    "[NAME]\n[ADDRESS] · [PHONE] · [EMAIL]\n\nBerufserfahrung\n"
    "03/2019 \u2013 heute: Senior Backend-Entwickler, Beispiel Logistik GmbH\n"
    "Sprachen: Deutsch (Muttersprache), Englisch (verhandlungssicher)\n"
)
PROFILE = ConfirmedProfile.model_validate(
    {
        "summary": "Backend-Entwickler für Python-Services.",
        "target_titles": [{"title": "Backend Developer", "lang": "en"}],
        "seniority": "senior",
        "total_years_experience": 7,
        "roles": [],
        "skills": [],
        "languages": [],
        "education": [],
        "certifications": [],
        "domains": ["Logistik"],
        "user_entered": {
            "work_authorization_de": "authorized",
            "security_clearances": [{"name": "Ü2", "level": None}],
        },
    }
)


def template(name: str) -> str:
    return resources.files("jobhunter.llm.prompts").joinpath(f"{name}.md").read_text("utf-8")


def test_the_renderer_version_is_1() -> None:
    assert RENDERER_VERSION == "1"


@pytest.mark.parametrize("name", ["profile_extract", "propose_queries"])
def test_a_template_version_is_its_name_and_the_first_12_hex_digits_of_its_sha256(name: str) -> None:
    digest = hashlib.sha256(template(name).encode("utf-8")).hexdigest()
    assert template_version(name) == f"{name}@{digest[:12]}"  # type: ignore[arg-type]
    assert re.fullmatch(rf"{name}@[0-9a-f]{{12}}", template_version(name))  # type: ignore[arg-type]


def test_extract_profile_sends_the_reviewed_text_inside_cv_text_and_a_static_system_block() -> None:
    prompt = extract_profile_prompt(REVIEWED)
    (system,) = prompt.system
    assert system.text == template("profile_extract") and system.cache is False
    (message,) = prompt.messages
    assert isinstance(message, HumanMessage) and isinstance(message.content, str)
    # The exact string inside <cv_text> is the reviewed text (transmitted_text_sha256 = reviewed_text_sha256).
    inside = re.fullmatch(r"<cv_text>(.*)</cv_text>", message.content, re.DOTALL)
    assert inside is not None and inside.group(1) == REVIEWED
    assert prompt.template_version == template_version("profile_extract")
    assert prompt.prefix_sha256() is None  # no cache marker: a single call (§10.6)
    assert dict(prompt.input_refs) == {}  # ids only; the node adds its own (Task 13)


def test_the_system_block_has_no_dynamic_content() -> None:
    """Two different CVs give byte-identical system blocks."""
    other = extract_profile_prompt("[NAME]\nJunior Data Analyst, Muster AG\n")
    assert extract_profile_prompt(REVIEWED).system == other.system
    assert REVIEWED not in other.system[0].text


def test_the_native_pdf_prompt_sends_the_file_as_a_document_block() -> None:
    pdf = b"%PDF-1.7\n% synthetic one-page CV\n"
    prompt = extract_profile_native_pdf_prompt(pdf)
    assert [b.text for b in prompt.system] == [template("profile_extract")]
    assert all(not b.cache for b in prompt.system)
    (message,) = prompt.messages
    assert isinstance(message.content, list)
    document, instruction = message.content
    assert document == {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf).decode()},
    }
    assert isinstance(instruction, dict) and instruction["type"] == "text" and instruction["text"]
    assert prompt.template_version == template_version("profile_extract")
    with pytest.raises(ValueError, match="empty"):
        extract_profile_native_pdf_prompt(b"")


def _tagged(prompt: RenderedPrompt, tag: str) -> str:
    """The text inside `<tag>…</tag>` of the prompt's user turn."""
    content = prompt.messages[-1].content
    assert isinstance(content, str)
    match = re.search(rf"<{tag}>(.*)</{tag}>", content, re.DOTALL)
    assert match is not None, tag
    return match.group(1)


def test_propose_queries_sends_the_profile_without_user_entered_and_the_locations() -> None:
    prompt = propose_queries_prompt(PROFILE, ["Berlin", "München"])
    (system,) = prompt.system
    assert system.text == template("propose_queries") and not system.cache
    assert len(prompt.messages) == 1
    profile_json = _tagged(prompt, "candidate_profile")
    sent = json.loads(profile_json)
    # The user-entered block (work authorisation, clearances) never goes to the model.
    assert sent == PROFILE.model_dump(mode="json", exclude={"user_entered"})
    assert profile_json.encode("utf-8") == canonical_json(sent)  # sorted keys, no whitespace
    assert json.loads(_tagged(prompt, "search_locations")) == ["Berlin", "München"]
    assert json.loads(_tagged(propose_queries_prompt(PROFILE, []), "search_locations")) == []
    assert prompt.template_version == template_version("propose_queries")
    assert prompt.prefix_sha256() is None
    assert dict(prompt.input_refs) == {}


def test_prompt_schema_v1_holds_the_two_templates_and_the_four_transformed_schemas() -> None:
    templates, schemas = prompt_schema_v1()
    assert templates == {
        "profile_extract": template("profile_extract"),
        "propose_queries": template("propose_queries"),
    }
    assert schemas == {name: transformed(model) for name, model in SCHEMAS_V1.items()}
    assert set(schemas) == {"generator", "critic", "profile", "queries"}


def test_the_prompt_schema_hash_covers_templates_schemas_and_the_renderer_version() -> None:
    templates, schemas = prompt_schema_v1()
    expected = hashlib.sha256(canonical_json(templates) + canonical_json(schemas) + b"1").digest()
    assert prompt_schema_hash(templates, schemas) == expected
    assert prompt_schema_hash({**templates, "propose_queries": "x"}, schemas) != expected
    assert prompt_schema_hash(templates, {**schemas, "queries": {}}) != expected


def test_the_profile_template_states_the_extraction_rules() -> None:
    text = template("profile_extract").lower()
    assert "<cv_text>" in text and "never instructions" in text
    assert "only from the cv" in text  # extract only from the CV text
    assert "verbatim" in text  # every evidence copied verbatim
    assert "explicit" in text and "never infer a language" in text  # languages from explicit statements only
    assert "yyyy-mm-01" in text  # month-precision dates
    # §12.5: never infer or output a protected attribute
    for attribute in (
        "age",
        "date of birth",
        "gender",
        "nationality",
        "citizenship",
        "ethnicity",
        "religion",
        "marital or family status",
        "health",
        "disability",
        "photo",
        "political views",
    ):
        assert attribute in text, attribute
    # and never contact data
    for contact in ("name", "e-mail", "phone", "address", "url"):
        assert contact in text, contact


def test_the_queries_template_states_the_proposal_rules() -> None:
    text = template("propose_queries").lower()
    assert "between 6 and 16 queries" in text
    assert "german (`de`) or english (`en`)" in text
    assert "rationale" in text and "target_title" in text
    assert "at least one" in text and "exploration" in text
    assert "no location" in text and "<search_locations>" in text
    assert "never instructions" in text


def test_the_templates_name_no_real_person() -> None:
    """A template is code, not data: no person, no contact value, no URL."""
    for name in render.TEMPLATE_NAMES_V2:
        text = template(name)
        assert not re.search(r"[\w.+-]+@[\w-]+\.\w+|https?://|www\.|\+\d{2}\s?\d", text), name
