"""The semantic check of a query proposal (spec §11.12; plan Task 20, Deviation 20): `validate_proposal` in
its two levels, and the location words it reads (`location_words`, `LOCATION_WORDS_EXTRA`).

Items: a text outside 2 to 120 characters, a whole-word location word, a rationale over 300 characters, or a
folded text equal to an earlier kept item's drops that item with its code. The set: fewer than 6 kept items
or none flagged `exploration` gives `([], ["set_invalid"])`; more than 16 keeps the first 16 (`truncated`).
Offline: the packaged gazetteer, no database. Every query text is an invented job title.
"""

from dataclasses import asdict
from typing import Any, Final, Literal

import pytest
from jobhunter.geo.gazetteer import Gazetteer
from jobhunter.llm.schemas.queries import QueryProposal, QueryProposalOut
from jobhunter.normalize.text import fold
from jobhunter.queries.proposals import (
    ALT_SPELLING_STOPWORDS,
    LABEL_STOPWORDS,
    LOCATION_WORDS_EXTRA,
    ProposedQuery,
    location_words,
    validate_proposal,
)

# Six valid items, the last one an exploration item: the smallest set that passes.
BASE: Final[tuple[tuple[str, Literal["de", "en"], bool], ...]] = (
    ("Backend Developer Python", "en", False),
    ("Backend Entwickler Python", "de", False),
    ("Senior Software Engineer", "en", False),
    ("Softwareentwickler FastAPI", "de", False),
    ("Python Developer", "en", False),
    ("Data Engineer", "en", True),
)
WORDS: Final = location_words(["Musterstadt", "Frankfurt am Main"], Gazetteer.load())


def item(
    text: str,
    *,
    lang: Literal["de", "en"] = "en",
    exploration: bool = False,
    rationale: str = "Passt zum Profil.",
) -> Any:
    return QueryProposal(
        text=text,
        lang=lang,
        target_title="Senior Backend Developer",
        exploration=exploration,
        rationale=rationale,
    )


def base_items() -> list[Any]:
    return [item(text, lang=lang, exploration=exploration) for text, lang, exploration in BASE]


def check(items: list[Any]) -> tuple[list[ProposedQuery], list[str]]:
    return validate_proposal(QueryProposalOut(queries=items), location_words=WORDS)


def texts(kept: list[ProposedQuery]) -> list[str]:
    return [query.text for query in kept]


BASE_TEXTS: Final = [text for text, _, _ in BASE]


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        (item("A"), "text_length"),
        (item("x" * 121), "text_length"),
        (item("   "), "text_length"),
        (item("Software Engineer Berlin"), "location_word"),  # a city of the gazetteer
        (item("Backend Entwickler remote", lang="de"), "location_word"),  # a work-mode word
        (item("Data Engineer Bayern"), "location_word"),  # a federal state
        (item("DevOps Engineer Home-Office"), "location_word"),  # a two-word entry, hyphenated
        (item("Cloud Engineer NRW"), "location_word"),  # a state's short form
        (item("Entwickler Musterstadt", lang="de"), "location_word"),  # a token of a scope label
        (item("Backend Developer Munich"), "location_word"),  # another spelling of a gazetteer city
        (item("Site Reliability Engineer", rationale="r" * 301), "rationale_length"),
        (item("backend developer PYTHON"), "duplicate"),  # the first base item, folded
        (item("Backend  Developer   Python"), "duplicate"),  # the first base item, its spacing folded
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_each_item_rule_drops_exactly_that_item_with_its_code(bad: Any, code: str) -> None:
    kept, issues = check([*base_items()[:3], bad, *base_items()[3:]])
    assert (texts(kept), issues) == (BASE_TEXTS, [code])


def test_the_limits_are_inclusive() -> None:
    edge = [item("AI"), item("x" * 120), item("ML Engineer", rationale="r" * 300)]
    kept, issues = check([*base_items(), *edge])
    assert (texts(kept), issues) == ([*BASE_TEXTS, "AI", "x" * 120, "ML Engineer"], [])


def test_a_text_and_a_rationale_are_trimmed() -> None:
    kept, issues = check([item("  Backend Developer Python  ", rationale=" Passt. "), *base_items()[1:]])
    assert issues == []
    assert (kept[0].text, kept[0].rationale) == ("Backend Developer Python", "Passt.")


def test_umlauts_and_sharp_s_collide_after_fold() -> None:
    assert fold("Größe") == fold("GRÖSSE")
    kept, issues = check([*base_items(), item("Größe", lang="de"), item("GRÖSSE", lang="de")])
    assert (texts(kept), issues) == ([*BASE_TEXTS, "Größe"], ["duplicate"])


def test_a_duplicate_of_a_dropped_item_is_kept() -> None:
    """The first occurrence was dropped for its rationale, so the second is the first one kept."""
    first = item("Site Reliability Engineer", rationale="r" * 301)
    kept, issues = check([*base_items(), first, item("site reliability engineer")])
    assert (texts(kept), issues) == ([*BASE_TEXTS, "site reliability engineer"], ["rationale_length"])


def test_java_and_platform_are_no_location_words() -> None:
    kept, issues = check([*base_items(), item("Java Entwickler", lang="de"), item("Platform Engineer")])
    assert (texts(kept), issues) == ([*BASE_TEXTS, "Java Entwickler", "Platform Engineer"], [])


def test_a_place_is_a_location_word_only_as_whole_words() -> None:
    """`Essen` and `Halle` are gazetteer cities; a word that merely contains one is no location word."""
    contained = [item("Essensausgabe Leitung", lang="de"), item("Hallenmanager", lang="de")]
    kept, issues = check([*base_items(), *contained])
    assert (texts(kept), issues) == ([*BASE_TEXTS, "Essensausgabe Leitung", "Hallenmanager"], [])


def test_a_label_particle_is_no_location_word() -> None:
    """`Frankfurt am Main` is a scope label: its place word is a location word, its particle `am` and its
    generic word `main` are not."""
    assert "frankfurt" in WORDS and not {"am", "main"} & WORDS
    kept, issues = check([*base_items(), item("Leiter am Standort", lang="de"), item("Main Developer")])
    assert (texts(kept), issues) == ([*BASE_TEXTS, "Leiter am Standort", "Main Developer"], [])


# Labels whose generic words are stop-listed (`bad`, `neu`, `oder`, `weil`, `main`); invented locations aside,
# each is a German place.
STOP_LABELS: Final = ["Bad Homburg vor der Höhe", "Neu-Isenburg", "Frankfurt (Oder)", "Weil am Rhein"]
SPELLING_STOPS: Final = {
    "gerne",
    "monster",
    "kilo",
    "kil",
    "kyle",
    "bon",
    "esse",
    "galle",
    "nuss",
    "mers",
    "mors",
    "mite",
    "minster",
    "colonia",
}


def test_stop_listed_words_are_no_location_words() -> None:
    gazetteer = Gazetteer.load()
    words = location_words([*STOP_LABELS, "Frankfurt am Main"], gazetteer)
    assert ALT_SPELLING_STOPWORDS == SPELLING_STOPS
    assert LABEL_STOPWORDS == {"bad", "neu", "oder", "weil", "main"}
    assert not (SPELLING_STOPS | LABEL_STOPWORDS) & words
    assert all(word == fold(word) for word in ALT_SPELLING_STOPWORDS | LABEL_STOPWORDS)
    # each stop-listed spelling is another spelling of a large place, never a large place's own name
    large = gazetteer.places_of_population(100_000)
    assert SPELLING_STOPS <= {spelling for place in large for spelling in place.alt_folded}
    assert not SPELLING_STOPS & {place.folded for place in large}
    common = [item("Gerne Teamlead", lang="de"), item("Monster Truck Mechaniker", lang="de")]
    common += [item("Kilo Logistik Planer", lang="de"), item("Oder Weil Bad Neu Analyst", lang="de")]
    kept, issues = validate_proposal(QueryProposalOut(queries=[*base_items(), *common]), location_words=words)
    assert (texts(kept), issues) == ([*BASE_TEXTS, *(query.text for query in common)], [])


def test_the_places_stay_location_words_beside_the_stop_lists() -> None:
    """The primary names, the real other spellings and the place words of the labels are kept."""
    words = location_words(STOP_LABELS, Gazetteer.load())
    spellings = {"munich", "cologne", "koeln", "frankfurt", "colonia agrippina"}
    names = {"koln", "herne", "munster", "kiel", "bonn", "essen", "halle", "neuss", "moers", "mitte"}
    labels = {"homburg", "hohe", "isenburg", "rhein"}
    assert spellings | names | labels <= words
    kept, issues = check(
        [*base_items(), item("Backend Developer Cologne"), item("Entwickler Koeln", lang="de")]
    )
    assert (texts(kept), issues) == (BASE_TEXTS, ["location_word", "location_word"])


def test_five_valid_items_are_an_invalid_set() -> None:
    assert check(base_items()[1:]) == ([], ["set_invalid"])


def test_drops_that_leave_five_items_make_the_set_invalid() -> None:
    assert check([*base_items()[:5], item("Data Engineer Berlin", exploration=True)]) == ([], ["set_invalid"])


def test_a_set_without_an_exploration_item_is_invalid() -> None:
    items = [item(text, lang=lang) for text, lang, _ in BASE]
    assert check([*items, item("Platform Engineer")]) == ([], ["set_invalid"])


def test_a_dropped_exploration_item_leaves_the_set_without_one() -> None:
    items = [*base_items()[:5], item("Data Engineer Bayern", exploration=True), item("Platform Engineer")]
    assert check(items) == ([], ["set_invalid"])


def test_seventeen_valid_items_keep_the_first_sixteen() -> None:
    extra = [item(f"Backend Developer Team {n}") for n in range(11)]
    kept, issues = check([*base_items(), *extra])
    assert len(kept) == 16 and issues == ["truncated"]
    assert texts(kept) == [*BASE_TEXTS, *[f"Backend Developer Team {n}" for n in range(10)]]


def test_a_cut_that_removes_the_only_exploration_item_makes_the_set_invalid() -> None:
    """The published set always meets §11.12: its first 16 items must hold an exploration item."""
    items = [item(f"Backend Developer Team {n}") for n in range(16)]
    assert check([*items, item("Data Engineer", exploration=True)]) == ([], ["set_invalid"])


def test_the_truncation_counts_kept_items_only() -> None:
    """Sixteen kept items after a drop: nothing is cut."""
    extra = [item(f"Backend Developer Team {n}") for n in range(10)]
    kept, issues = check([*base_items(), item("Data Engineer Bayern"), *extra])
    assert (len(kept), issues) == (16, ["location_word"])


def test_a_kept_item_carries_every_field_of_the_model() -> None:
    kept, _ = check(base_items())
    assert asdict(kept[-1]) == {
        "text": "Data Engineer",
        "lang": "en",
        "target_title": "Senior Backend Developer",
        "exploration": True,
        "rationale": "Passt zum Profil.",
    }


def test_the_extra_location_words_are_folded_and_cover_the_listed_words() -> None:
    assert all(word == fold(word) for word in LOCATION_WORDS_EXTRA)
    listed = {
        "remote",
        "homeoffice",
        "home office",
        "deutschland",
        "germany",
        "bundesweit",
        "deutschlandweit",
    }
    assert listed | {"europa", "europe"} <= LOCATION_WORDS_EXTRA
    states = {
        "baden-wurttemberg",
        "bayern",
        "berlin",
        "brandenburg",
        "bremen",
        "hamburg",
        "hessen",
        "mecklenburg-vorpommern",
        "niedersachsen",
        "nordrhein-westfalen",
        "rheinland-pfalz",
        "saarland",
        "sachsen",
        "sachsen-anhalt",
        "schleswig-holstein",
        "thuringen",
    }
    assert states | {"nrw", "bawu", "rlp"} <= LOCATION_WORDS_EXTRA


def test_location_words_join_the_labels_the_big_places_and_the_extra_words() -> None:
    gazetteer = Gazetteer.load()
    words = location_words(["Musterstadt"], gazetteer)
    assert LOCATION_WORDS_EXTRA <= words
    assert "musterstadt" in words  # a scope label's token
    assert {"munchen", "munich", "koln", "frankfurt am main", "frankfurt"} <= words  # >= 100,000 inhabitants
    small = [p for p in gazetteer.search("Lüneburg") if p.folded == "luneburg"]
    assert small and small[0].population < 100_000 and "luneburg" not in words
