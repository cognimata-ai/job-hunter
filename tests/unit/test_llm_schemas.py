"""The structured-output schemas (M2 Task 6): each structural mirror's JSON schema is the spec's, compared as
canonical JSON with the spec text below (generator §11.5, critic §11.7.3, profile §12.4, queries §11.12; each
literal is the spec's code block, unedited). The mirrors carry no limits (§11.6 stage 1), forbid extra keys
and refuse a value outside an enum; the transformed schema the gateway sends keeps every `enum`, `anyOf` and
`$ref`.
"""

# ruff: noqa: E501  (the schema literals are the spec's code blocks as they are)

import json
from collections.abc import Iterator
from typing import Any

import anthropic
import pytest
from jobhunter.db.seed import canonical_json
from jobhunter.llm.gateway import output_schema
from jobhunter.llm.schemas import SCHEMAS, json_schema, transformed
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.generator import GeneratorOut
from jobhunter.llm.schemas.profile import ConfirmedProfile, ProfileOut, UserEntered
from jobhunter.llm.schemas.queries import QueryProposalOut
from pydantic import BaseModel, ValidationError

SPEC_SCHEMAS: dict[str, Any] = {
    name: json.loads(text)
    for name, text in {
        "generator": """\
{
  "type": "object", "additionalProperties": false,
  "required": ["not_a_job_ad", "injection_observed", "job_facts", "criteria", "summary_en", "summary_de"],
  "properties": {
    "not_a_job_ad": {"type": "boolean"},
    "injection_observed": {"type": "boolean"},
    "job_facts": {"$ref": "#/$defs/job_facts"},
    "criteria": {
      "type": "object", "additionalProperties": false,
      "required": ["role_fit", "skills", "experience_seniority", "domain", "languages", "education_certifications"],
      "properties": {
        "role_fit": {"$ref": "#/$defs/criterion"},
        "skills": {"$ref": "#/$defs/criterion"},
        "experience_seniority": {"$ref": "#/$defs/criterion"},
        "domain": {"$ref": "#/$defs/criterion"},
        "languages": {"$ref": "#/$defs/criterion"},
        "education_certifications": {"$ref": "#/$defs/criterion"}
      }
    },
    "summary_en": {"type": "string"},
    "summary_de": {"type": "string"}
  },
  "$defs": {
    "quotes": {"type": "array", "items": {"type": "string"}},
    "level": {"anyOf": [{"type": "integer", "enum": [0, 1, 2, 3, 4]}, {"type": "null"}]},
    "profile_ref": {
      "type": "object", "additionalProperties": false, "required": ["section", "quote"],
      "properties": {
        "section": {"type": "string", "enum": ["summary", "target_titles", "roles", "skills", "languages",
                                                "education", "certifications", "domains", "seniority"]},
        "quote": {"type": "string"}
      }
    },
    "criterion": {
      "type": "object", "additionalProperties": false,
      "required": ["level", "basis", "ad_evidence", "profile_evidence", "rationale"],
      "properties": {
        "level": {"$ref": "#/$defs/level"},
        "basis": {"type": "string", "enum": ["observed", "unknown", "unconstrained"]},
        "ad_evidence": {"$ref": "#/$defs/quotes"},
        "profile_evidence": {"type": "array", "items": {"$ref": "#/$defs/profile_ref"}},
        "rationale": {"type": "string"}
      }
    },
    "cefr": {"type": "string", "enum": ["A1", "A2", "B1", "B2", "C1", "C2", "native", "unspecified"]},
    "lang_code": {"type": "string", "enum": ["de", "en", "fr", "es", "it", "nl", "pl", "pt", "other"]},
    "nullable_string": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    "nullable_int": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    "requirement": {
      "type": "object", "additionalProperties": false, "required": ["text", "evidence"],
      "properties": {"text": {"type": "string"}, "evidence": {"$ref": "#/$defs/quotes"}}
    },
    "job_facts": {
      "type": "object", "additionalProperties": false,
      "required": ["work_mode", "onsite_only", "remote_region", "required_languages", "must_haves",
                   "nice_to_haves", "seniority", "min_years_experience", "industry", "employment_types",
                   "degree", "certifications", "security_clearance", "work_permit"],
      "properties": {
        "work_mode": {"type": "object", "additionalProperties": false, "required": ["value", "evidence"],
          "properties": {"value": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unspecified"]},
                         "evidence": {"$ref": "#/$defs/quotes"}}},
        "onsite_only": {"type": "object", "additionalProperties": false, "required": ["asserted", "evidence"],
          "properties": {"asserted": {"type": "boolean"}, "evidence": {"$ref": "#/$defs/quotes"}}},
        "remote_region": {"type": "object", "additionalProperties": false,
          "required": ["value", "countries", "evidence"],
          "properties": {"value": {"type": "string", "enum": ["germany", "dach", "eu_eea", "europe", "worldwide",
                                                               "other_country", "unspecified"]},
                         "countries": {"type": "array", "items": {"type": "string"}},
                         "evidence": {"$ref": "#/$defs/quotes"}}},
        "required_languages": {"type": "array", "items": {
          "type": "object", "additionalProperties": false,
          "required": ["language", "language_other", "level", "mandatory", "evidence"],
          "properties": {"language": {"$ref": "#/$defs/lang_code"},
                         "language_other": {"$ref": "#/$defs/nullable_string"},
                         "level": {"$ref": "#/$defs/cefr"},
                         "mandatory": {"type": "boolean"},
                         "evidence": {"$ref": "#/$defs/quotes"}}}},
        "must_haves": {"type": "array", "items": {"$ref": "#/$defs/requirement"}},
        "nice_to_haves": {"type": "array", "items": {"$ref": "#/$defs/requirement"}},
        "seniority": {"type": "object", "additionalProperties": false, "required": ["value", "evidence"],
          "properties": {"value": {"type": "string", "enum": ["intern", "working_student", "apprentice", "junior",
                           "mid", "senior", "lead", "principal", "head", "unspecified"]},
                         "evidence": {"$ref": "#/$defs/quotes"}}},
        "min_years_experience": {"type": "object", "additionalProperties": false, "required": ["value", "evidence"],
          "properties": {"value": {"$ref": "#/$defs/nullable_int"}, "evidence": {"$ref": "#/$defs/quotes"}}},
        "industry": {"type": "object", "additionalProperties": false, "required": ["value", "evidence"],
          "properties": {"value": {"$ref": "#/$defs/nullable_string"}, "evidence": {"$ref": "#/$defs/quotes"}}},
        "employment_types": {"type": "object", "additionalProperties": false, "required": ["values", "evidence"],
          "properties": {"values": {"type": "array", "items": {"type": "string", "enum": ["full_time",
                           "part_time", "contract", "temporary", "internship", "working_student",
                           "apprenticeship", "freelance"]}},
                         "evidence": {"$ref": "#/$defs/quotes"}}},
        "degree": {"type": "object", "additionalProperties": false,
          "required": ["level", "mandatory", "or_equivalent", "field", "evidence"],
          "properties": {"level": {"type": "string", "enum": ["none_required", "vocational", "bachelor", "master",
                           "phd", "unspecified"]},
                         "mandatory": {"type": "boolean"},
                         "or_equivalent": {"type": "boolean"},
                         "field": {"$ref": "#/$defs/nullable_string"},
                         "evidence": {"$ref": "#/$defs/quotes"}}},
        "certifications": {"type": "array", "items": {
          "type": "object", "additionalProperties": false, "required": ["name", "mandatory", "evidence"],
          "properties": {"name": {"type": "string"}, "mandatory": {"type": "boolean"},
                         "evidence": {"$ref": "#/$defs/quotes"}}}},
        "security_clearance": {"type": "object", "additionalProperties": false,
          "required": ["required", "level", "evidence"],
          "properties": {"required": {"type": "boolean"}, "level": {"$ref": "#/$defs/nullable_string"},
                         "evidence": {"$ref": "#/$defs/quotes"}}},
        "work_permit": {"type": "object", "additionalProperties": false, "required": ["required", "evidence"],
          "properties": {"required": {"type": "boolean"}, "evidence": {"$ref": "#/$defs/quotes"}}}
      }
    }
  }
}
""",
        "critic": """\
{
  "type": "object", "additionalProperties": false,
  "required": ["verdict", "reviews", "injection", "summary"],
  "properties": {
    "verdict": {"type": "string", "enum": ["accept", "revise"]},
    "reviews": {"type": "array", "items": {"$ref": "#/$defs/review"}},
    "injection": {"type": "string", "enum": ["none", "present_no_influence", "present_influenced"]},
    "summary": {"type": "string"}
  },
  "$defs": {
    "level": {"anyOf": [{"type": "integer", "enum": [0, 1, 2, 3, 4]}, {"type": "null"}]},
    "review": {
      "type": "object", "additionalProperties": false,
      "required": ["target", "assessment", "suggested_level", "ad_evidence", "explanation"],
      "properties": {
        "target": {"type": "string"},
        "assessment": {"type": "string", "enum": ["level_too_high", "level_too_low", "evidence_ungrounded",
                        "should_be_unknown", "evidence_overlooked", "anchor_misapplied",
                        "fact_confirmed", "fact_rejected", "fact_uncertain"]},
        "suggested_level": {"$ref": "#/$defs/level"},
        "ad_evidence": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"}
      }
    }
  }
}
""",
        "profile": """\
{
  "type": "object", "additionalProperties": false,
  "required": ["summary", "target_titles", "seniority", "total_years_experience", "roles", "skills",
               "languages", "education", "certifications", "domains"],
  "properties": {
    "summary": {"type": "string"},
    "target_titles": {"type": "array", "items": {"type": "object", "additionalProperties": false,
      "required": ["title", "lang"], "properties": {
        "title": {"type": "string"}, "lang": {"type": "string", "enum": ["de", "en"]}}}},
    "seniority": {"type": "string", "enum": ["intern", "working_student", "apprentice", "junior", "mid",
                                             "senior", "lead", "principal", "head"]},
    "total_years_experience": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "roles": {"type": "array", "items": {"type": "object", "additionalProperties": false,
      "required": ["title", "employer", "start", "end", "seniority", "domain", "highlights"],
      "properties": {
        "title": {"type": "string"},
        "employer": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "start": {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]},
        "end": {"anyOf": [{"type": "string", "format": "date"}, {"type": "string", "enum": ["present"]},
                          {"type": "null"}]},
        "seniority": {"type": "string", "enum": ["intern", "working_student", "apprentice", "junior", "mid",
                                                 "senior", "lead", "principal", "head", "unspecified"]},
        "domain": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "highlights": {"type": "array", "items": {"type": "string"}}}}},
    "skills": {"type": "array", "items": {"type": "object", "additionalProperties": false,
      "required": ["name", "category", "level", "years", "evidence"],
      "properties": {
        "name": {"type": "string"},
        "category": {"type": "string", "enum": ["programming_language", "framework", "tool", "platform",
                                                "method", "domain_knowledge", "soft_skill", "other"]},
        "level": {"type": "string", "enum": ["basic", "intermediate", "advanced", "expert"]},
        "years": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "evidence": {"type": "string"}}}},
    "languages": {"type": "array", "items": {"type": "object", "additionalProperties": false,
      "required": ["code", "code_other", "cefr", "evidence"],
      "properties": {
        "code": {"type": "string", "enum": ["de", "en", "fr", "es", "it", "nl", "pl", "pt", "other"]},
        "code_other": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "cefr": {"type": "string", "enum": ["A1", "A2", "B1", "B2", "C1", "C2", "native", "unspecified"]},
        "evidence": {"type": "string"}}}},
    "education": {"type": "array", "items": {"type": "object", "additionalProperties": false,
      "required": ["level", "field", "institution", "year"],
      "properties": {
        "level": {"type": "string", "enum": ["vocational", "bachelor", "master", "phd", "other"]},
        "field": {"type": "string"},
        "institution": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "year": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}},
    "certifications": {"type": "array", "items": {"type": "object", "additionalProperties": false,
      "required": ["name", "issuer", "year"],
      "properties": {
        "name": {"type": "string"},
        "issuer": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "year": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}},
    "domains": {"type": "array", "items": {"type": "string"}}
  }
}
""",
        "queries": """\
{
  "type": "object", "additionalProperties": false, "required": ["queries"],
  "properties": {
    "queries": {"type": "array", "items": {
      "type": "object", "additionalProperties": false,
      "required": ["text", "lang", "target_title", "exploration", "rationale"],
      "properties": {
        "text": {"type": "string"},
        "lang": {"type": "string", "enum": ["de", "en"]},
        "target_title": {"type": "string"},
        "exploration": {"type": "boolean"},
        "rationale": {"type": "string"}
      }}}
  }
}
""",
    }.items()
}


MODELS: dict[str, type[BaseModel]] = {
    "generator": GeneratorOut,
    "critic": CriticOut,
    "profile": ProfileOut,
    "queries": QueryProposalOut,
}


def test_the_schema_registry_holds_the_four_structured_outputs() -> None:
    assert dict(SCHEMAS) == MODELS


@pytest.mark.parametrize("name", sorted(MODELS))
def test_each_json_schema_is_the_spec_schema(name: str) -> None:
    assert canonical_json(json_schema(MODELS[name])) == canonical_json(SPEC_SCHEMAS[name])


@pytest.mark.parametrize("name", sorted(MODELS))
def test_the_request_sends_the_transformed_spec_schema(name: str) -> None:
    """What langchain's `with_structured_output` and the gateway's byte bound send (`transform_schema` of the
    model class, which reads `model_json_schema`) is `transformed(model)`."""
    model = MODELS[name]
    assert model.model_json_schema() == json_schema(model)
    assert anthropic.transform_schema(model) == transformed(model) == output_schema(model)
    assert transformed(model) == anthropic.transform_schema(SPEC_SCHEMAS[name])


def _keywords(schema: Any, path: str = "") -> Iterator[tuple[str, str, str]]:
    """Every `enum`, `anyOf` (its variant count), `$ref`, `required` and `additionalProperties` of `schema`,
    with its path."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in ("enum", "$ref", "required", "additionalProperties"):
                yield path, key, json.dumps(value)
            elif key == "anyOf":
                yield path, key, str(len(value))
            yield from _keywords(value, f"{path}/{key}")
    elif isinstance(schema, list):
        for i, item in enumerate(schema):
            yield from _keywords(item, f"{path}/{i}")


# The keywords each spec schema uses: the queries schema has no `anyOf`, only two have `$defs`.
KEYWORDS = {
    "generator": {"enum", "anyOf", "$ref"},
    "critic": {"enum", "anyOf", "$ref"},
    "profile": {"enum", "anyOf"},
    "queries": {"enum"},
}


@pytest.mark.parametrize("name", sorted(MODELS))
def test_transformed_keeps_enum_anyof_and_ref(name: str) -> None:
    sent = transformed(MODELS[name])
    assert set(_keywords(sent)) == set(_keywords(SPEC_SCHEMAS[name]))
    kinds = {key for _, key, _ in _keywords(sent)}
    assert KEYWORDS[name] <= kinds  # so the comparison above is not vacuous
    # Nothing was outside the API's subset: `transform_schema` would have moved it into a description.
    assert '"description"' not in json.dumps(sent)


def _skill(i: int) -> dict[str, Any]:
    return {
        "name": f"Skill {i}",
        "category": "tool",
        "level": "advanced",
        "years": 2,
        "evidence": f"Skill {i} im Einsatz",
    }


def profile_body(**overrides: Any) -> dict[str, Any]:
    """A structurally valid profile of an invented candidate (no name, no contact)."""
    body: dict[str, Any] = {
        "summary": "Backend-Entwickler mit Schwerpunkt auf Python-Services und Datenbanken.",
        "target_titles": [{"title": "Backend Developer", "lang": "en"}],
        "seniority": "senior",
        "total_years_experience": 7.5,
        "roles": [
            {
                "title": "Senior Backend-Entwickler",
                "employer": "Beispiel Logistik GmbH",
                "start": "2019-03-01",
                "end": "present",
                "seniority": "senior",
                "domain": "Logistik",
                "highlights": ["Tourenplanung mit Python und PostgreSQL neu gebaut"],
            },
            {
                "title": "Werkstudent Softwareentwicklung",
                "employer": None,
                "start": None,
                "end": None,
                "seniority": "unspecified",
                "domain": None,
                "highlights": [],
            },
        ],
        "skills": [_skill(0)],
        "languages": [
            {"code": "de", "code_other": None, "cefr": "native", "evidence": "Deutsch (Muttersprache)"},
            {"code": "other", "code_other": "Swahili", "cefr": "A2", "evidence": "Swahili (Grundkenntnisse)"},
        ],
        "education": [{"level": "master", "field": "Informatik", "institution": None, "year": 2016}],
        "certifications": [{"name": "Zertifikat Datenbanken", "issuer": None, "year": None}],
        "domains": ["Logistik"],
    }
    body.update(overrides)
    return body


def test_profile_out_carries_no_limits() -> None:
    """Limits are the semantic validator's (§12.4): 200 skills, an empty title list and an empty summary are
    structurally valid."""
    out = ProfileOut.model_validate(profile_body(skills=[_skill(i) for i in range(200)]))
    assert len(out.skills) == 200
    assert ProfileOut.model_validate(profile_body(target_titles=[], summary="")).target_titles == []


def test_profile_out_keeps_month_dates_and_present_as_strings() -> None:
    """`YYYY-MM` and unparsable dates are the validator's to repair (Task 12), never a structural failure."""
    roles = [{**profile_body()["roles"][0], "start": "2019-03", "end": "kein Datum"}]
    out = ProfileOut.model_validate(profile_body(roles=roles))
    assert (out.roles[0].start, out.roles[0].end) == ("2019-03", "kein Datum")
    assert ProfileOut.model_validate(profile_body()).roles[0].end == "present"


@pytest.mark.parametrize(
    "body",
    [
        profile_body(nationality="deutsch"),  # an extra top-level key: no §12.5 attribute can be stored
        profile_body(skills=[{**_skill(0), "age": 41}]),  # an extra nested key
        profile_body(seniority="staff"),  # not in the enum
        profile_body(seniority="unspecified"),  # only a role's seniority may be unspecified
        profile_body(skills=[{**_skill(0), "category": "language"}]),
        profile_body(languages=[{"code": "sv", "code_other": None, "cefr": "B1", "evidence": "Schwedisch"}]),
        profile_body(languages=[{"code": "de", "code_other": None, "cefr": "C3", "evidence": "Deutsch"}]),
        profile_body(target_titles=[{"title": "Backend Developer", "lang": "fr"}]),
        {k: v for k, v in profile_body().items() if k != "domains"},  # a required key missing
        profile_body(total_years_experience="7"),  # a string is no number
        profile_body(
            education=[{"level": "master", "field": "Informatik", "institution": None, "year": "2016"}]
        ),
    ],
)
def test_profile_out_refuses_extra_keys_unknown_enums_and_wrong_types(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProfileOut.model_validate(body)


def test_a_model_draft_validates_as_a_confirmed_profile_with_the_user_entered_defaults() -> None:
    """The LLM never produces `user_entered` (§12.4): its stored output is a `ConfirmedProfile` with the
    defaults until the user enters them."""
    confirmed = ConfirmedProfile.model_validate(profile_body())
    assert confirmed.user_entered == UserEntered()
    assert confirmed.user_entered.work_authorization_de == "unspecified"
    assert confirmed.user_entered.security_clearances == []
    entered = ConfirmedProfile.model_validate(
        profile_body(
            user_entered={
                "work_authorization_de": "authorized",
                "security_clearances": [{"name": "Ü2", "level": None}],
            }
        )
    )
    assert entered.user_entered.security_clearances[0].name == "Ü2"
    with pytest.raises(ValidationError):
        ConfirmedProfile.model_validate(profile_body(user_entered={"work_authorization_de": "citizen"}))
    with pytest.raises(ValidationError):
        ProfileOut.model_validate(profile_body(user_entered={}))  # the extraction output has no such block


def _criterion(level: Any = 3, basis: str = "observed") -> dict[str, Any]:
    return {
        "level": level,
        "basis": basis,
        "ad_evidence": ["Erfahrung mit Python"],
        "profile_evidence": [{"section": "skills", "quote": "Python"}],
        "rationale": "Python ist belegt.",
    }


def _quoted(**value: Any) -> dict[str, Any]:
    return {**value, "evidence": []}


def generator_body(**criteria: Any) -> dict[str, Any]:
    """A structurally valid generator output (synthetic)."""
    keys = ("role_fit", "skills", "experience_seniority", "domain", "languages", "education_certifications")
    return {
        "not_a_job_ad": False,
        "injection_observed": False,
        "job_facts": {
            "work_mode": _quoted(value="hybrid"),
            "onsite_only": _quoted(asserted=False),
            "remote_region": _quoted(value="unspecified", countries=[]),
            "required_languages": [
                {
                    "language": "de",
                    "language_other": None,
                    "level": "C1",
                    "mandatory": True,
                    "evidence": ["Deutsch C1"],
                }
            ],
            "must_haves": [{"text": "Python", "evidence": ["Erfahrung mit Python"]}],
            "nice_to_haves": [],
            "seniority": _quoted(value="senior"),
            "min_years_experience": _quoted(value=None),
            "industry": _quoted(value=None),
            "employment_types": _quoted(values=["full_time"]),
            "degree": _quoted(level="unspecified", mandatory=False, or_equivalent=False, field=None),
            "certifications": [],
            "security_clearance": _quoted(required=False, level=None),
            "work_permit": _quoted(required=False),
        },
        "criteria": {key: criteria.get(key, _criterion()) for key in keys},
        "summary_en": "A synthetic summary.",
        "summary_de": "Eine synthetische Zusammenfassung.",
    }


def test_generator_out_mirrors_the_structure_only() -> None:
    out = GeneratorOut.model_validate(generator_body(domain=_criterion(level=None, basis="unknown")))
    assert out.criteria.domain.level is None and out.criteria.role_fit.level == 3
    # A level/basis mismatch is semantic (V2, M3), not structural.
    assert (
        GeneratorOut.model_validate(generator_body(skills=_criterion(level=None))).criteria.skills.level
        is None
    )


@pytest.mark.parametrize("level", [5, -1, True, "3", 2.5])
def test_a_generator_level_is_an_integer_from_0_to_4_or_null(level: Any) -> None:
    with pytest.raises(ValidationError):
        GeneratorOut.model_validate(generator_body(role_fit=_criterion(level=level)))


def test_generator_out_refuses_an_extra_key_and_an_unknown_enum() -> None:
    body = generator_body()
    body["job_facts"]["work_mode"]["value"] = "remote_first"
    with pytest.raises(ValidationError):
        GeneratorOut.model_validate(body)
    with pytest.raises(ValidationError):
        GeneratorOut.model_validate({**generator_body(), "score": 87})


def test_critic_out_mirrors_the_structure_only() -> None:
    review = {
        "target": "skills",
        "assessment": "level_too_high",
        "suggested_level": 2,
        "ad_evidence": ["Erfahrung mit Kubernetes"],
        "explanation": "Kubernetes fehlt im Profil.",
    }
    body = {"verdict": "revise", "reviews": [review], "injection": "none", "summary": "Synthetisch."}
    assert CriticOut.model_validate(body).reviews[0].suggested_level == 2
    for bad in ({**body, "verdict": "reject"}, {**body, "reviews": [{**review, "suggested_level": True}]}):
        with pytest.raises(ValidationError):
            CriticOut.model_validate(bad)


def test_query_proposal_out_mirrors_the_structure_only() -> None:
    item = {
        "text": "Backend Entwickler Python",
        "lang": "de",
        "target_title": "Backend Developer",
        "exploration": False,
        "rationale": "Kernrolle des Profils.",
    }
    # 2 items and an empty rationale are structurally valid: the 6 to 16 and length rules are semantic (§11.12).
    assert len(QueryProposalOut.model_validate({"queries": [item, {**item, "rationale": ""}]}).queries) == 2
    for bad in ({"queries": [{**item, "lang": "fr"}]}, {"queries": [{**item, "location": "Berlin"}]}):
        with pytest.raises(ValidationError):
            QueryProposalOut.model_validate(bad)
