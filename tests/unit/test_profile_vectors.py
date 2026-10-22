"""Task 19: the texts a profile version is embedded as (spec §12.6 step 3; plan Task 19).

`vector_texts` is the one place that decides which vectors a version has: `("summary", summary)` and
`("title:<i>", target_titles[i].title)`, `i` counted from 0 in the stored list. The embedder, the write and
the `profile_vectors` reconciler all read it, so its shape is pinned here without a database.
"""

from typing import Any

from jobhunter.embeddings import BATCH
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.profile import schema, vectors


def profile_of(summary: str, titles: list[tuple[str, str]]) -> ConfirmedProfile:
    """A valid profile of invented content with `summary` and the `(title, lang)` target titles."""
    body: dict[str, Any] = {
        "summary": summary,
        "target_titles": [{"title": title, "lang": lang} for title, lang in titles],
        "seniority": "senior",
        "total_years_experience": None,
        "roles": [],
        "skills": [],
        "languages": [],
        "education": [],
        "certifications": [],
        "domains": [],
    }
    return ConfirmedProfile.model_validate(body)


def test_the_summary_comes_first_then_each_title_by_its_zero_based_index() -> None:
    profile = profile_of(
        "Erfahrene Entwicklerin.", [("Backend Developer", "en"), ("Backend-Entwicklerin", "de")]
    )
    assert vectors.vector_texts(profile) == [
        ("summary", "Erfahrene Entwicklerin."),
        ("title:0", "Backend Developer"),
        ("title:1", "Backend-Entwicklerin"),
    ]


def test_the_text_of_a_title_is_its_title_and_not_the_object() -> None:
    [_, (kind, text)] = vectors.vector_texts(profile_of("Summary text.", [("Data Engineer", "en")]))
    assert (kind, text) == ("title:0", "Data Engineer")  # not {"title": …, "lang": …}, not the language


def test_empty_texts_are_skipped_and_the_other_kinds_keep_their_indices() -> None:
    profile = profile_of("", [("", "en"), ("Platform Engineer", "en"), ("   ", "de"), ("SRE", "en")])
    assert vectors.vector_texts(profile) == [("title:1", "Platform Engineer"), ("title:3", "SRE")]


def test_a_profile_without_texts_has_no_vector() -> None:
    assert vectors.vector_texts(profile_of("", [])) == []


def test_the_dedup_key_names_the_kind_and_the_version() -> None:
    assert vectors.KIND == "embed_profile"
    assert vectors.dedup_key(17) == "embed_profile:17"


def test_a_validated_profile_always_fits_one_embed_batch() -> None:
    """The handler sends every text in one request (the service refuses more than `BATCH`): the summary and
    the most target titles §12.4 allows."""
    most_titles = schema.TARGET_TITLES[1]
    assert 1 + most_titles <= BATCH
