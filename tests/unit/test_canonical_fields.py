from datetime import UTC, datetime, timedelta

import pytest
from jobhunter.db.seed import content_hash
from jobhunter.normalize.fields import (
    SOURCE_RANK,
    CanonicalFields,
    ObservationView,
    WatchlistDisplay,
    select_canonical,
)
from jobhunter.scoring.filters import det_facts_hash

from tests.support.facts import AMBIGUOUS_WITH_ONE_DE_CANDIDATE, BERLIN, HAMBURG, LONDON
from tests.support.observations import DESCRIPTION, obs, predicted, stated

T = datetime(2026, 10, 16, 9, tzinfo=UTC)
ENGLISH = "We are looking for an engineer who will work with the team and the product in our office. " * 4
LLM_INPUT = {
    "title",
    "company_display",
    "locations",
    "work_mode_possible",
    "work_mode_source",
    "employment_types",
    "employment_source",
    "ad_language",
    "description_completeness",
    "description_text",
}


def test_description_and_title_precedence() -> None:
    adz = obs(
        "adzuna",
        1,
        T,
        description_text="x" * 500 + "…",
        description_completeness="partial",
        title_raw="Senior Engineer (m/w/d)",
    )
    js = obs(
        "jsearch", 2, T - timedelta(hours=5), description_text="full text " * 100, title_raw="Senior Engineer"
    )
    gh = obs(
        "greenhouse",
        3,
        T - timedelta(days=1),
        description_text="ats text " * 100,
        title_raw="Senior Engineer - Berlin",
    )
    f = select_canonical([adz, js, gh], first_seen_at=T, watchlist_display=WatchlistDisplay("Acme GmbH", gh))
    assert (
        f.description_text.startswith("ats text")
        and f.description_completeness == "full"
        and f.title == "Senior Engineer - Berlin"
    )
    assert (
        f.field_provenance["description_text"].posting_id == 3
        and f.company_display == "Acme GmbH"
        and f.source_ids == ("adzuna", "greenhouse", "jsearch")
    )
    assert set(f.llm_input) == LLM_INPUT
    # a partial never overwrites a full
    assert select_canonical([adz, js], first_seen_at=T, watchlist_display=None).description_text.startswith(
        "full text"
    )


def test_work_mode_intersection_salary_and_dates() -> None:
    a = obs(
        "lever",
        1,
        T,
        work_mode_possible=frozenset({"remote", "hybrid"}),
        work_mode_source="structured",
        salary=stated(60000, 80000),
    )
    b = obs(
        "recruitee",
        2,
        T,
        work_mode_possible=frozenset({"hybrid"}),
        work_mode_source="structured",
        salary=predicted(50000, 70000),
    )
    f = select_canonical([a, b], first_seen_at=T, watchlist_display=None)
    assert (
        f.work_mode_possible == frozenset({"hybrid"})
        and f.salary is not None
        and f.salary.kind == "stated"
        and f.conflicts == ()
    )
    c = obs("ashby", 3, T, work_mode_possible=frozenset({"onsite"}), work_mode_source="structured")
    g = select_canonical([a, c], first_seen_at=T, watchlist_display=None)
    assert g.conflicts == ("work_mode_possible",) and g.work_mode_possible == frozenset({"remote", "hybrid"})
    unknown = obs(
        "jsearch", 1, T, work_mode_possible=frozenset({"remote", "hybrid", "onsite"}), work_mode_source="none"
    )
    assert select_canonical([unknown], first_seen_at=T, watchlist_display=None).work_mode_source == "none"


def test_future_provider_date_is_clamped_to_first_seen_at() -> None:  # Review Focus 2
    f = select_canonical(
        [obs("jsearch", 1, T, posted_at=T + timedelta(hours=10), posted_precision="hour")],
        first_seen_at=T,
        watchlist_display=None,
    )
    assert (f.posted_at_effective, f.posted_precision) == (T, "hour")
    g = select_canonical(
        [
            obs("adzuna", 1, T, posted_at=T - timedelta(days=3), posted_precision="ingest"),
            obs("greenhouse", 2, T, posted_at=T - timedelta(days=5), posted_precision="exact"),
        ],
        first_seen_at=T,
        watchlist_display=None,
    )
    assert (g.posted_at_effective, g.posted_precision) == (T - timedelta(days=5), "exact")
    unknown = obs("serpapi", 1, T, posted_at=None, posted_precision="unknown")
    assert select_canonical([unknown], first_seen_at=T, watchlist_display=None).posted_precision == "unknown"


def test_det_facts_hash_ignores_the_description() -> None:
    # `ad_language` is a det fact, detected from the description when no structured language is given ("a"
    # is an English stop word, "b" none), so both observations state the same structured language.
    a = select_canonical(
        [obs("jsearch", 1, T, description_text="A " * 100, ad_language_structured="en")],
        first_seen_at=T,
        watchlist_display=None,
    )
    b = select_canonical(
        [obs("jsearch", 1, T, description_text="B " * 100, ad_language_structured="en")],
        first_seen_at=T,
        watchlist_display=None,
    )
    assert a.det_facts_hash == b.det_facts_hash and a.material_hash != b.material_hash
    detected = [
        select_canonical(
            [obs("jsearch", 1, T, description_text=text)], first_seen_at=T, watchlist_display=None
        )
        for text in ("A " * 100, "B " * 100)
    ]
    assert [d.ad_language for d in detected] == ["en", "unknown"]
    assert detected[0].det_facts_hash != detected[1].det_facts_hash


# -- the other rows of the §8.8 table ------------------------------------------------------------------------


def test_source_rank_is_the_seeded_precedence() -> None:
    ats = ("greenhouse", "lever", "ashby", "personio", "recruitee", "smartrecruiters")
    assert {source: SOURCE_RANK[source] for source in ats} == dict.fromkeys(ats, 1)
    assert (
        SOURCE_RANK["jsearch"],
        SOURCE_RANK["serpapi"],
        SOURCE_RANK["arbeitnow"],
        SOURCE_RANK["adzuna"],
    ) == (
        2,
        2,
        3,
        4,
    )


def test_ties_go_to_the_most_recent_observation_then_the_lowest_posting_id() -> None:
    older = obs("lever", 1, T - timedelta(hours=1), title_raw="Older")
    newer = obs("ashby", 3, T, title_raw="Newer")
    twin = obs("greenhouse", 2, T, title_raw="Twin")
    f = select_canonical([older, newer, twin], first_seen_at=T, watchlist_display=None)
    assert f.title == "Twin" and f.field_provenance["title"].posting_id == 2
    assert select_canonical([twin, older, newer], first_seen_at=T, watchlist_display=None) == f  # input order
    aggregator = obs("jsearch", 4, T + timedelta(hours=1), title_raw="Aggregator")
    assert select_canonical([older, aggregator], first_seen_at=T, watchlist_display=None).title == "Older"


def test_the_title_gives_title_norm_and_seniority_tokens() -> None:
    f = select_canonical(
        [obs("jsearch", 1, T, title_raw="Sr. Backend-Entwickler (m/w/d)")],
        first_seen_at=T,
        watchlist_display=None,
    )
    assert (f.title, f.title_norm, f.seniority_tokens) == (
        "Sr. Backend-Entwickler",
        "senior backend entwickler",
        frozenset({"senior"}),
    )
    assert f.field_provenance["title_norm"] == f.field_provenance["title"]


def test_a_garbage_description_is_used_only_without_an_alternative() -> None:
    garbage = obs("greenhouse", 1, T, description_text="null\nnull\nnull\n" + "garbage text " * 30)
    snippet = obs("adzuna", 2, T, description_text="y" * 400 + "…", description_completeness="partial")
    assert garbage.quality == "garbage" and snippet.quality == "ok"
    f = select_canonical([garbage, snippet], first_seen_at=T, watchlist_display=None)
    assert (f.description_text[0], f.description_completeness, f.description_quality) == (
        "y",
        "partial",
        "ok",
    )
    alone = select_canonical([garbage], first_seen_at=T, watchlist_display=None)
    # §8.9: with no alternative the job is `partial` with quality `garbage`
    assert (alone.description_quality, alone.description_completeness, alone.completeness) == (
        "garbage",
        "partial",
        "partial",
    )
    short = obs("greenhouse", 3, T, description_text="short text " * 10)
    ok = obs("jsearch", 4, T - timedelta(days=1), description_text="full text " * 100)
    assert short.quality == "short"
    assert (
        select_canonical([short, ok], first_seen_at=T, watchlist_display=None)
        .field_provenance["description_text"]
        .posting_id
        == 4
    )


def test_company_display_watchlist_then_ats_then_the_most_frequent_raw_form() -> None:
    js = obs("jsearch", 1, T, company_raw="ACME Corp")
    sp = obs("serpapi", 2, T - timedelta(hours=1), company_raw="Acme")
    an = obs("arbeitnow", 3, T - timedelta(hours=2), company_raw="Acme")
    f = select_canonical([js, sp, an], first_seen_at=T, watchlist_display=None)
    assert (f.company_display, f.company_key, f.field_provenance["company_display"].posting_id) == (
        "Acme",
        "acme",
        2,
    )
    gh = obs("greenhouse", 4, T - timedelta(days=1), company_raw="Acme Group")
    g = select_canonical([js, sp, an, gh], first_seen_at=T, watchlist_display=None)
    assert (g.company_display, g.company_key) == ("Acme Group", "acme group")
    w = select_canonical(
        [js, gh], first_seen_at=T, watchlist_display=WatchlistDisplay("Acme Software GmbH", gh)
    )
    assert (w.company_display, w.company_key) == ("Acme Software GmbH", "acme software")
    assert (w.field_provenance["company_display"].posting_id, w.field_provenance["company_display"].rule) == (
        4,
        "watchlist_display_name",
    )


def test_the_watchlist_name_is_credited_to_the_observation_that_supplied_it() -> None:
    newer = obs("greenhouse", 1, T, company_raw="Acme Group")  # the best-ranked one, on no watchlist board
    board = obs("lever", 2, T - timedelta(days=1), company_raw="Acme")
    f = select_canonical(
        [newer, board], first_seen_at=T, watchlist_display=WatchlistDisplay("Acme Software GmbH", board)
    )
    assert f.company_display == "Acme Software GmbH"
    assert f.field_provenance["company_display"].posting_id == 2 == f.lineage["company_display"].posting_id
    with pytest.raises(ValueError, match="not one of the observations"):
        select_canonical([newer], first_seen_at=T, watchlist_display=WatchlistDisplay("Acme", board))


def test_locations_are_the_ats_union_or_the_best_ranked_parsed_ones() -> None:
    gh = obs("greenhouse", 1, T, locations=[BERLIN], location_kind="city")
    lever_berlin = BERLIN.model_copy(update={"source": "lever"})
    lv = obs("lever", 2, T - timedelta(hours=1), locations=[lever_berlin, HAMBURG], location_kind="multi")
    js = obs("jsearch", 3, T, locations=[LONDON], location_kind="city")
    f = select_canonical([gh, lv, js], first_seen_at=T, watchlist_display=None)
    assert ([location.label for location in f.locations], f.location_kind) == (["Berlin", "Hamburg"], "multi")
    assert f.llm_input["locations"] == ["Berlin", "Hamburg"]
    assert [p.posting_id for p in f.contributors["locations"]] == [1, 2]
    # no ATS location: the best-ranked source's parsed locations, every candidate kept
    adz = obs("adzuna", 4, T, locations=[LONDON], location_kind="city")
    sp = obs(
        "serpapi", 5, T - timedelta(days=1), locations=[AMBIGUOUS_WITH_ONE_DE_CANDIDATE], location_kind="city"
    )
    g = select_canonical([adz, sp], first_seen_at=T, watchlist_display=None)
    assert g.locations == (AMBIGUOUS_WITH_ONE_DE_CANDIDATE,) and len(g.locations[0].candidates) == 2
    assert g.location_kind == "city" and g.field_provenance["locations"].posting_id == 5
    # no location at all: a marker of the best-ranked source that has one
    remote = obs("arbeitnow", 6, T, location_kind="remote")
    h = select_canonical([remote, obs("adzuna", 7, T)], first_seen_at=T, watchlist_display=None)
    assert (h.locations, h.location_kind) == ((), "remote")
    nothing = select_canonical([obs("adzuna", 7, T)], first_seen_at=T, watchlist_display=None)
    assert (nothing.locations, nothing.location_kind, nothing.field_provenance["locations"].rule) == (
        (),
        "unknown",
        "none",
    )


def test_the_location_union_does_not_depend_on_recency() -> None:
    lever_berlin = BERLIN.model_copy(update={"source": "lever"})  # the same place as another board states it

    def union(gh_at: datetime, lv_at: datetime) -> CanonicalFields:
        gh = obs("greenhouse", 1, gh_at, locations=[HAMBURG, BERLIN], location_kind="multi")
        lv = obs("lever", 2, lv_at, locations=[lever_berlin], location_kind="city")
        return select_canonical([gh, lv], first_seen_at=T, watchlist_display=None)

    before, after = union(T, T - timedelta(hours=1)), union(T - timedelta(hours=1), T)
    for f in (before, after):
        assert f.locations == (BERLIN, HAMBURG)  # by folded label; a repeated place from the lowest posting
        assert f.llm_input["locations"] == ["Berlin", "Hamburg"]
    assert before.material_hash == after.material_hash and before.det_facts_hash == after.det_facts_hash


def test_work_mode_text_derived_without_a_structured_source() -> None:
    sp = obs("serpapi", 1, T, work_mode_possible=frozenset({"remote", "hybrid"}), work_mode_source="text")
    f = select_canonical([sp, obs("adzuna", 2, T)], first_seen_at=T, watchlist_display=None)
    assert (f.work_mode_possible, f.work_mode_source) == (frozenset({"remote", "hybrid"}), "text")
    an = obs(
        "arbeitnow",
        3,
        T - timedelta(days=1),
        work_mode_possible=frozenset({"remote"}),
        work_mode_source="structured",
    )
    g = select_canonical([sp, an], first_seen_at=T, watchlist_display=None)
    assert (g.work_mode_possible, g.work_mode_source) == (frozenset({"remote"}), "structured")
    assert g.llm_input["work_mode_possible"] == ["remote"] and g.llm_input["work_mode_source"] == "structured"


def test_the_intersection_names_every_structured_posting_and_a_conflict_only_the_kept_one() -> None:
    a = obs("lever", 1, T, work_mode_possible=frozenset({"remote", "hybrid"}), work_mode_source="structured")
    b = obs("jsearch", 2, T, work_mode_possible=frozenset({"remote"}), work_mode_source="structured")
    f = select_canonical([a, b], first_seen_at=T, watchlist_display=None)
    assert f.work_mode_possible == frozenset({"remote"})
    assert [p.posting_id for p in f.contributors["work_mode_possible"]] == [1, 2]
    assert f.contributors["work_mode_source"] == f.contributors["work_mode_possible"]
    c = obs("ashby", 3, T, work_mode_possible=frozenset({"onsite"}), work_mode_source="structured")
    g = select_canonical([a, b, c], first_seen_at=T, watchlist_display=None)
    assert g.work_mode_possible == frozenset({"remote", "hybrid"}) and g.conflicts == ("work_mode_possible",)
    assert [p.posting_id for p in g.contributors["work_mode_possible"]] == [1]


def test_remote_countries_are_the_union_of_the_structured_values() -> None:
    lv = obs("lever", 1, T, remote_countries=("DE",))
    rc = obs("recruitee", 2, T, remote_countries=("AT", "DE"))
    f = select_canonical([lv, rc, obs("jsearch", 3, T)], first_seen_at=T, watchlist_display=None)
    assert f.remote_countries == ("AT", "DE") and f.det_facts.remote_countries == ("AT", "DE")


def test_employment_structured_then_adzuna_then_parsed_strings() -> None:
    gh = obs("greenhouse", 1, T, employment_types=("full_time",), employment_source="structured")
    adz = obs("adzuna", 2, T, employment_types=("part_time",), employment_source="structured")
    sp = obs("serpapi", 3, T, employment_types=("contract",), employment_source="text")
    none = obs("jsearch", 4, T)

    def employment(*views: ObservationView) -> tuple[tuple[str, ...], str]:
        f = select_canonical(views, first_seen_at=T, watchlist_display=None)
        return f.employment_types, f.employment_source

    assert employment(gh, adz, sp, none) == (("full_time",), "structured")
    assert employment(adz, sp, none) == (("part_time",), "structured")
    assert employment(sp, none) == (("contract",), "text")
    assert employment(none) == ((), "none")


def test_salary_stated_before_predicted_then_by_source() -> None:
    ats = obs("lever", 1, T, salary=stated(60000, 80000))
    serp = obs("serpapi", 2, T, salary=stated(50000, 60000))
    adz_stated = obs("adzuna", 3, T, salary=stated(40000, 50000))
    adz_predicted = obs("adzuna", 4, T + timedelta(hours=1), salary=predicted(70000, 90000))
    candidates = [ats, serp, adz_stated, adz_predicted]
    winners = []
    while candidates:
        f = select_canonical(candidates, first_seen_at=T, watchlist_display=None)
        winners.append(f.field_provenance["salary"].posting_id)
        candidates = [c for c in candidates if c.posting_id != winners[-1]]
    assert winners == [1, 2, 3, 4]
    assert select_canonical([obs("jsearch", 5, T)], first_seen_at=T, watchlist_display=None).salary is None


def test_posted_date_best_precision_then_source_rank() -> None:
    js = obs("jsearch", 1, T, posted_at=T - timedelta(hours=3), posted_precision="hour")
    jd = obs("jsearch", 2, T, posted_at=T - timedelta(days=1), posted_precision="day")
    sp = obs("serpapi", 3, T, posted_at=T - timedelta(days=2), posted_precision="relative_day")
    an = obs("arbeitnow", 4, T, posted_at=T - timedelta(days=4), posted_precision="exact")
    gh = obs("lever", 5, T - timedelta(days=9), posted_at=T - timedelta(days=6), posted_precision="exact")
    f = select_canonical([js, jd, sp, an, gh], first_seen_at=T, watchlist_display=None)
    assert (f.posted_at_effective, f.posted_precision) == (T - timedelta(days=6), "exact")
    g = select_canonical([js, jd, sp, an], first_seen_at=T, watchlist_display=None)
    assert g.field_provenance["posted_at_effective"].posting_id == 4
    h = select_canonical([jd, sp, js], first_seen_at=T, watchlist_display=None)
    assert h.posted_precision == "hour" and h.posted_at_effective == T - timedelta(hours=3)


def test_ad_language_structured_then_detected_then_unknown() -> None:
    english = obs("jsearch", 1, T, description_text=ENGLISH)
    gh = obs("greenhouse", 2, T, source_structured={"language": "de"})
    f = select_canonical([english, gh], first_seen_at=T, watchlist_display=None)
    assert (f.ad_language, f.field_provenance["ad_language"].rule) == ("de", "structured")
    other = obs("greenhouse", 3, T, source_structured={"language": "fr"})
    assert select_canonical([english, other], first_seen_at=T, watchlist_display=None).ad_language == "other"
    regional = obs("smartrecruiters", 4, T, ad_language_structured="en-GB")
    assert select_canonical([regional], first_seen_at=T, watchlist_display=None).ad_language == "en"
    d = select_canonical([english], first_seen_at=T, watchlist_display=None)
    assert (d.ad_language, d.field_provenance["ad_language"].rule) == ("en", "detected")
    assert d.field_provenance["ad_language"].posting_id == d.field_provenance["description_text"].posting_id
    neutral = select_canonical([obs("jsearch", 5, T)], first_seen_at=T, watchlist_display=None)
    assert neutral.description_text == DESCRIPTION
    assert (neutral.ad_language, neutral.field_provenance["ad_language"].rule) == ("unknown", "unknown")


def test_industry_ats_then_personio_occupation_then_adzuna_category() -> None:
    adz = obs("adzuna", 1, T, source_structured={"industry": "it-jobs"})
    per = obs("personio", 2, T, source_structured={"occupationCategory": "Software Development"})
    sr = obs("smartrecruiters", 3, T - timedelta(days=1), source_structured={"industry": "Internet"})

    def industry(*views: ObservationView) -> str | None:
        return select_canonical(views, first_seen_at=T, watchlist_display=None).industry_structured

    assert (industry(adz, per, sr), industry(adz, per), industry(adz), industry(obs("jsearch", 4, T))) == (
        "Internet",
        "Software Development",
        "it-jobs",
        None,
    )


def test_structured_seniority_by_precedence_rank_then_recency_then_posting_id() -> None:  # carry 1
    sr = obs("smartrecruiters", 1, T - timedelta(days=1), source_structured={"seniority": "mid_senior_level"})
    per = obs("personio", 2, T, source_structured={"seniority": "experienced"})
    per_old = obs("personio", 3, T - timedelta(days=2), source_structured={"seniority": "student"})
    adz = obs("adzuna", 4, T + timedelta(hours=1), source_structured={"seniority": "director"})
    f = select_canonical(
        [sr, per, per_old, adz, obs("jsearch", 5, T)], first_seen_at=T, watchlist_display=None
    )
    assert (f.structured_seniority, f.det_facts.structured_seniority) == ("experienced", "experienced")
    assert (
        f.field_provenance["structured_seniority"].posting_id,
        f.field_provenance["structured_seniority"].rule,
    ) == (
        2,
        "structured",
    )
    twin = obs("personio", 6, T - timedelta(days=1), source_structured={"seniority": "student"})
    assert select_canonical([twin, sr], first_seen_at=T, watchlist_display=None).structured_seniority == (
        "mid_senior_level"
    )
    none = select_canonical([obs("jsearch", 5, T)], first_seen_at=T, watchlist_display=None)
    assert none.structured_seniority is None and none.det_facts.structured_seniority is None
    assert none.det_facts_hash != f.det_facts_hash


def test_det_facts_material_projection_and_lineage() -> None:
    gh = obs(
        "greenhouse",
        1,
        T,
        locations=[BERLIN],
        location_kind="city",
        employment_types=("part_time", "full_time"),
        employment_source="structured",
        salary=stated(60000, 80000),
        posted_at=T - timedelta(days=1),
        posted_precision="exact",
    )
    f = select_canonical([gh, obs("adzuna", 2, T)], first_seen_at=T, watchlist_display=None)
    facts = f.det_facts
    assert (facts.company_key, facts.title_norm, facts.locations, facts.salary) == (
        f.company_key,
        f.title_norm,
        f.locations,
        f.salary,
    )
    assert (
        facts.employment_types == frozenset({"full_time", "part_time"})
        and facts.posted_at_effective == f.posted_at_effective
    )
    assert f.det_facts_hash == det_facts_hash(facts)
    assert f.llm_input == {
        "title": "Python Developer",
        "company_display": "Acme",
        "locations": ["Berlin"],
        "work_mode_possible": ["hybrid", "onsite", "remote"],
        "work_mode_source": "none",
        "employment_types": ["full_time", "part_time"],
        "employment_source": "structured",
        "ad_language": "unknown",
        "description_completeness": "full",
        "description_text": f.description_text,
    }
    assert f.material_hash == content_hash(f.llm_input)
    assert set(f.lineage) == LLM_INPUT == set(f.contributors)
    assert all(f.contributors[field][0] == f.lineage[field] for field in LLM_INPUT)
    assert f.lineage["description_text"] == f.field_provenance["description_text"]
    assert f.lineage["work_mode_source"] == f.lineage["work_mode_possible"]
    assert f.lineage["employment_source"] == f.lineage["employment_types"]
    assert f.lineage["description_completeness"] == f.lineage["description_text"]


def test_select_canonical_needs_an_observation() -> None:
    with pytest.raises(ValueError, match="at least one observation"):
        select_canonical([], first_seen_at=T, watchlist_display=None)
