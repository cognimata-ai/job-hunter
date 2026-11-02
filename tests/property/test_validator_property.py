"""`validate_draft` over random drafts (spec §11.6: "semantic validation … produces issues and deterministic
failures, never exceptions"; M3 Task 5). For random drafts and contexts:

- `validate_draft` never raises;
- every `Failure.key` is a criterion key, a fact id `derive_fact_ids` gives for the corrected draft (or
  `not_a_job_ad:ad`), or the pseudo key of an id-less fact: its code, or `<code>:<n>` for a list item;
- the corrected draft is a fixed point: validating it again (with its stored history) repairs nothing and
  changes nothing;
- a deterministic `experience_seniority` (Controller Ruling T5-c) raises no issue or failure of its own: no
  level, basis or evidence failure, no quote count, and a failing quote only where the generator's own
  profile reference stands; a recomputed one carries at most 2 ad quotes, every one grounded;
- `repair_for_publication` never raises on it.

The `PublishedResult` clause is Task 6's property test. Every datum is generated or invented.
"""

from typing import Any, get_args

from hypothesis import event, given, settings
from hypothesis import strategies as st
from jobhunter.llm.schemas.generator import Cefr, GeneratorDraft, LangCode
from jobhunter.scoring.facts import NOT_A_JOB_AD, derive_fact_ids
from jobhunter.scoring.validator import (
    DraftValidation,
    Normalisation,
    ValidationContext,
    repair_for_publication,
    validate_draft,
)

from tests.support.validation import CRITERIA, SHARED, ctx_for, synthetic_ad

CEFR = get_args(get_args(Cefr)[0])
CODES = get_args(get_args(LangCode)[0])
SINGLE_PSEUDO_KEYS = frozenset(
    {
        "work_mode",
        "onsite_only",
        "remote_region",
        "seniority",
        "min_years_experience",
        "industry",
        "employment_type",
        "degree",
        "security_clearance",
        "work_permit",
    }
)
LISTS = (
    ("must_haves", "must_have"),
    ("nice_to_haves", "nice_to_have"),
    ("required_languages", "required_language"),
    ("certifications", "certification_item"),
)
QUOTES = (
    "Python, PostgreSQL",
    "Berlin, hybrid",
    "full_time (structured)",
    "Senior",
    "Mid-Level",
    "3 Jahre",
    "nicht im Text",
    "Python … PostgreSQL",
    "ab",
    "- - -",
    "x" * 301,
    "",
)
NAMES = ("Französisch", "FRANZÖSISCH", "Scrum Master", "scrum  master", "", "Zertifikat " * 20, "AWS\x07Pro")

quotes = st.lists(st.sampled_from(QUOTES), max_size=3)
texts = st.sampled_from(("", "Python", "Kubernetes", "Wort " * 70, "x" * 250))
names = st.sampled_from(NAMES)


def _criterion() -> st.SearchStrategy[dict[str, Any]]:
    return st.fixed_dictionaries(
        {
            "level": st.none() | st.integers(0, 4),
            "basis": st.sampled_from(("observed", "unknown", "unconstrained")),
            "ad_evidence": quotes,
            "profile_evidence": st.lists(
                st.fixed_dictionaries(
                    {
                        "section": st.sampled_from(("skills", "domains", "seniority", "hobbies")),
                        "quote": st.sampled_from(("Python", "Logistik", "senior", "nichts davon")),
                    }
                ),
                max_size=3,
            ),
            "rationale": texts,
        }
    )


@st.composite
def drafts(draw: st.DrawFn) -> GeneratorDraft:
    requirement = st.fixed_dictionaries({"text": texts, "evidence": quotes})
    doc = {
        "not_a_job_ad": draw(st.booleans()),
        "injection_observed": draw(st.booleans()),
        "job_facts": {
            "work_mode": {
                "value": draw(st.sampled_from(("remote", "hybrid", "onsite", "unspecified"))),
                "evidence": draw(quotes),
            },
            "onsite_only": {"asserted": draw(st.booleans()), "evidence": draw(quotes)},
            "remote_region": {
                "value": draw(
                    st.sampled_from(
                        ("germany", "dach", "eu_eea", "europe", "worldwide", "other_country", "unspecified")
                    )
                ),
                "countries": draw(st.lists(st.sampled_from(("DE", "AT", "CH", "XX", "de", "")), max_size=4)),
                "evidence": draw(quotes),
            },
            "required_languages": draw(
                st.lists(
                    st.fixed_dictionaries(
                        {
                            "language": st.sampled_from(CODES),
                            "language_other": st.none() | names,
                            "level": st.sampled_from(CEFR),
                            "mandatory": st.booleans(),
                            "evidence": quotes,
                        }
                    ),
                    max_size=4,
                )
            ),
            "must_haves": draw(st.lists(requirement, max_size=4)),
            "nice_to_haves": draw(st.lists(requirement, max_size=2)),
            "seniority": {
                "value": draw(st.sampled_from(("intern", "junior", "mid", "senior", "head", "unspecified"))),
                "evidence": draw(quotes),
            },
            "min_years_experience": {
                "value": draw(st.none() | st.integers(-3, 60)),
                "evidence": draw(quotes),
            },
            "industry": {"value": draw(st.none() | names), "evidence": draw(quotes)},
            "employment_types": {
                "values": draw(
                    st.lists(st.sampled_from(("full_time", "part_time", "freelance")), max_size=2)
                ),
                "evidence": draw(quotes),
            },
            "degree": {
                "level": draw(st.sampled_from(("none_required", "bachelor", "master", "unspecified"))),
                "mandatory": draw(st.booleans()),
                "or_equivalent": draw(st.booleans()),
                "field": draw(st.none() | names),
                "evidence": draw(quotes),
            },
            "certifications": draw(
                st.lists(
                    st.fixed_dictionaries({"name": names, "mandatory": st.booleans(), "evidence": quotes}),
                    max_size=3,
                )
            ),
            "security_clearance": {
                "required": draw(st.booleans()),
                "level": draw(st.none() | names),
                "evidence": draw(quotes),
            },
            "work_permit": {"required": draw(st.booleans()), "evidence": draw(quotes)},
        },
        "criteria": {key: draw(_criterion()) for key in CRITERIA},
        "summary_en": draw(texts),
        "summary_de": draw(texts),
    }
    return GeneratorDraft.model_validate(doc)


_LONG = synthetic_ad("x" * 19_990 + " Senior. " + SHARED + " Mid-Level, 3 Jahre." * 300)
CONTEXTS: tuple[ValidationContext, ...] = (
    ctx_for(synthetic_ad(f"Senior oder Mid-Level, 3 Jahre. {SHARED}")),
    ctx_for(synthetic_ad(title="Backend Engineer"), structured_work_modes=("onsite", "remote")),
    ctx_for(_LONG, structured_work_modes=()),
)


def allowed_keys(draft: GeneratorDraft) -> set[str]:
    """Every key a failure of `draft` may name, derived from the rules, not from the module."""
    keys = set(CRITERIA) | set(derive_fact_ids(draft.job_facts)) | {NOT_A_JOB_AD.fact_id} | SINGLE_PSEUDO_KEYS
    facts = draft.job_facts.model_dump()
    for field, code in LISTS:
        keys |= {f"{code}:{i}" for i in range(len(facts[field]))}
    return keys


@settings(max_examples=300, deadline=None)
@given(draft=drafts(), ctx=st.sampled_from(CONTEXTS))
def test_validate_draft_never_raises_and_names_only_the_drafts_keys(
    draft: GeneratorDraft, ctx: ValidationContext
) -> None:
    v = validate_draft(draft, ctx, normalisation=Normalisation())
    keys = allowed_keys(v.draft)
    assert {f.key for f in v.failures} <= keys
    again = validate_draft(v.draft, ctx, normalisation=Normalisation(), history=v.history)
    assert again.repairs == []
    assert again.draft == v.draft
    assert again.failures == v.failures and again.issues == v.issues
    assert again.history == v.history
    assert_the_seniority_correction_raises_nothing(draft, v)
    repair_for_publication(v.draft, v)


SENIORITY = "experience_seniority"
_LEVEL_AND_EVIDENCE = frozenset(
    {
        "unknown_with_level",
        "level_missing",
        "unconstrained_not_4",
        "ad_evidence_missing",
        "profile_evidence_missing",
    }
)


def assert_the_seniority_correction_raises_nothing(draft: GeneratorDraft, v: DraftValidation) -> None:
    repair = next((r for r in v.repairs if r.code == "seniority_recomputed"), None)
    corrected = v.draft.criteria.experience_seniority
    event("seniority: " + ("none" if repair is None else "unknown" if corrected.level is None else "rated"))
    if repair is None:
        return
    original = draft.criteria.experience_seniority
    assert not [f for f in v.failures if f.key == SENIORITY and f.code in _LEVEL_AND_EVIDENCE]
    path = f"criteria.{SENIORITY}"
    assert not [i for i in v.issues if i.path.startswith(f"{path}.") and i.code == "too_many_quotes"]
    assert not [i for i in v.issues if i.path.startswith(f"{path}.ad_evidence")]  # its ad quotes all ground
    for issue in v.issues:
        if issue.path.startswith(f"{path}.profile_evidence["):  # only the generator's own reference fails
            index = int(issue.path.removeprefix(f"{path}.profile_evidence[").rstrip("]"))
            assert corrected.profile_evidence[index] in original.profile_evidence
    if corrected.level is None:
        assert (corrected.basis, corrected.ad_evidence, corrected.profile_evidence, repair.exempt) == (
            "unknown",
            [],
            [],
            (),
        )
    else:
        assert corrected.basis == "observed" and len(corrected.ad_evidence) <= 2
        assert set(repair.exempt) == {"ad_evidence_missing", "profile_evidence_missing", "too_many_quotes"}
