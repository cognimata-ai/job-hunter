import math

import pytest
from jobhunter.geo.gazetteer import NON_DE_COLUMNS, PLACE_COLUMNS, Gazetteer
from jobhunter.sources.parsing.locations import (
    Candidate,
    Location,
    ParsedLocations,
    location_from_coordinates,
    parse_jsearch_location,
    parse_location_text,
    positively_outside_germany,
)


def parse(text: str) -> ParsedLocations:
    return parse_location_text(text, source="x")


def labels(text: str) -> list[str]:
    return [loc.label for loc in parse(text).locations]


def countries(text: str) -> list[list[str]]:
    return [[c.country_code for c in loc.candidates] for loc in parse(text).locations]


def test_resolution_keeps_every_candidate() -> None:
    p = parse_location_text("Neustadt", source="x")
    assert p.locations[0].resolution == "ambiguous" and len(p.locations[0].candidates) > 3
    assert parse_location_text("10115 Berlin", source="x").locations[0].resolution == "unique"
    u = parse_location_text("Hauptsitz Nord", source="x")
    assert u.locations[0].resolution == "unresolved" and not u.positively_non_de


def test_markers_and_multi() -> None:
    assert parse_location_text("Deutschlandweit", source="x").kind == "country_wide"
    assert parse_location_text("Remote", source="x").kind == "remote"
    m = parse_location_text("Berlin; Hamburg / München", source="x")
    assert m.kind == "multi" and [loc.label for loc in m.locations] == ["Berlin", "Hamburg", "München"]


def test_jsearch_location_strings() -> None:
    assert parse_jsearch_location("Berlin     •  über Stepstone", None, None).locations[0].label == "Berlin"
    assert parse_jsearch_location("Beliebiger Ort   •  über LinkedIn", None, None).kind == "country_wide"
    assert parse_jsearch_location(None, "München", "DE").locations[0].candidates[0].country_code == "DE"


def test_positive_non_de_evidence_only() -> None:
    assert positively_outside_germany(parse_location_text("Zürich", source="x").locations) is True
    assert positively_outside_germany(parse_location_text("London or Berlin", source="x").locations) is False
    assert positively_outside_germany(parse_location_text("", source="x").locations) is False
    assert parse_location_text("Wien", source="x").positively_non_de


# --- resolution -------------------------------------------------------------------------------


def test_a_unique_name_has_one_german_candidate_with_coordinates() -> None:
    p = parse("München")
    assert p.kind == "city" and not p.positively_non_de
    (loc,) = p.locations
    assert (loc.label, loc.resolution, loc.postal_code, loc.source) == ("München", "unique", None, "x")
    (c,) = loc.candidates
    assert c.country_code == "DE" and c.gazetteer_id == "geonames:2867714"
    assert 48.0 < c.lat < 48.3 and 11.4 < c.lon < 11.8


def test_spellings_and_case_resolve_to_the_same_place() -> None:
    ids = {parse(t).locations[0].candidates[0].gazetteer_id for t in ("Köln", "KOELN", "koln", "Cologne")}
    assert len(ids) == 1
    assert parse("Munich").locations[0].candidates == parse("München").locations[0].candidates


def test_homonyms_keep_german_and_foreign_candidates_and_are_not_positively_foreign() -> None:
    parsed = parse("Bergen")
    (loc,) = parsed.locations
    assert loc.resolution == "ambiguous" and "NO" in {c.country_code for c in loc.candidates}
    assert sum(c.country_code == "DE" for c in loc.candidates) >= 3
    assert loc.candidates[0].country_code == "DE" and not parsed.positively_non_de
    assert positively_outside_germany(parsed.locations) is False


def test_postal_code_resolves_before_the_name() -> None:
    p = parse("10115 Berlin")
    (loc,) = p.locations
    assert loc.resolution == "unique" and loc.postal_code == "10115" and loc.label == "10115 Berlin"
    assert [c.gazetteer_id for c in loc.candidates] == ["geonames:2950159"]
    assert parse("D-80331 München").locations[0].postal_code == "80331"
    assert parse("10115").locations[0].resolution == "unique"


def test_an_unknown_postal_code_falls_back_to_the_name_and_is_kept() -> None:
    (loc,) = parse("00000 Hamburg").locations
    assert loc.resolution == "unique" and loc.postal_code == "00000"
    assert loc.candidates[0].gazetteer_id == parse("Hamburg").locations[0].candidates[0].gazetteer_id
    (nameless,) = parse("00000").locations
    assert nameless.resolution == "unresolved" and nameless.postal_code == "00000"


@pytest.fixture
def small_gazetteer(monkeypatch: pytest.MonkeyPatch) -> Gazetteer:
    """Alpha and Beta are 6.8 km apart, Gamma is far away; the codes name one, two near or two far places."""
    rows = [
        "geonames:1\tAlpha\talpha\t\tDE\t05\t50.0\t8.0\t5000\t11111|22222|33333",
        "geonames:2\tBeta\tbeta\t\tDE\t05\t50.05\t8.05\t9000\t11111",
        "geonames:3\tGamma\tgamma\t\tDE\t02\t51.0\t9.0\t7000\t22222",
    ]
    non_de = ["Vienna\twien\tAT\tcity\tgeonames:9\t48.2\t16.4", "Austria\taustria\tAT\tcountry\t\t\t"]
    g = Gazetteer.from_tsv(
        "\n".join(["\t".join(PLACE_COLUMNS), *rows, ""]), "\n".join(["\t".join(NON_DE_COLUMNS), *non_de, ""])
    )
    monkeypatch.setattr(Gazetteer, "load", classmethod(lambda cls: g))
    return g


def test_places_that_share_a_code_and_lie_within_10_km_are_one_location_at_their_centroid(
    small_gazetteer: Gazetteer,
) -> None:
    (loc,) = parse("11111").locations
    assert loc.resolution == "unique"
    (c,) = loc.candidates
    assert c.gazetteer_id == "geonames:2"  # the most populous of them identifies the location
    assert (c.lat, c.lon) == pytest.approx((50.025, 8.025))


def test_places_that_share_a_code_and_lie_farther_apart_stay_ambiguous(small_gazetteer: Gazetteer) -> None:
    (loc,) = parse("22222").locations
    assert loc.resolution == "ambiguous"
    assert sorted(c.gazetteer_id for c in loc.candidates) == ["geonames:1", "geonames:3"]
    assert [c.gazetteer_id for c in parse("33333").locations[0].candidates] == ["geonames:1"]


def test_unknown_names_are_unresolved_labels_and_never_foreign() -> None:
    for text in ("Hauptsitz Nord", "Berlin Mitte", "Frankfurt a. M.", "Deutschland", "Germany", "Irgendwo"):
        (loc,) = parse(text).locations
        assert loc.resolution == "unresolved" and loc.candidates == [] and loc.label == text
        assert not parse(text).positively_non_de


# --- splitting and suffixes -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Berlin; Hamburg", ["Berlin", "Hamburg"]),
        ("Berlin;Hamburg", ["Berlin", "Hamburg"]),
        ("Berlin/Hamburg", ["Berlin", "Hamburg"]),
        ("Berlin und Hamburg", ["Berlin", "Hamburg"]),
        ("Berlin oder Hamburg", ["Berlin", "Hamburg"]),
        ("Berlin or Hamburg", ["Berlin", "Hamburg"]),
        ("Berlin AND Hamburg", ["Berlin", "Hamburg"]),
        ("Berlin; Hamburg und München / Köln", ["Berlin", "Hamburg", "München", "Köln"]),
        ("Berlin; ; Hamburg;", ["Berlin", "Hamburg"]),
        ("Berlin / Berlin", ["Berlin"]),
        ("Frankfurt/Oder", ["Frankfurt/Oder"]),  # a place name as it stands
        ("Frankfurt (Oder) / Berlin", ["Frankfurt (Oder)", "Berlin"]),
        ("Berlin (Mitte/Kreuzberg) / Hamburg", ["Berlin", "Hamburg"]),  # the slash is inside parentheses
        ("n/a", ["n/a"]),
        ("-", []),
    ],
)
def test_split(text: str, expected: list[str]) -> None:
    assert labels(text) == expected


def test_kind_is_the_shape_of_the_location_list() -> None:
    assert [parse(t).kind for t in ("", "-", "Berlin", "Hauptsitz Nord", "Berlin; Hamburg")] == [
        "unknown",
        "unknown",
        "city",
        "city",
        "multi",
    ]
    assert parse("").locations == [] and not parse("").positively_non_de


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("Berlin • über Stepstone", "Berlin"),
        ("Berlin • über LinkedIn • über Indeed", "Berlin"),
        ("Hamburg über Stepstone", "Hamburg"),
        ("München via Indeed", "München"),
        ("Berlin (Stepstone)", "Berlin"),
        ("Berlin (Stepstone) • über X", "Berlin"),
        ("Frankfurt (Oder)", "Frankfurt (Oder)"),  # the parenthesis belongs to the name
        ("Halle (Saale)", "Halle (Saale)"),
    ],
)
def test_publisher_suffixes_are_dropped_and_name_parentheses_kept(text: str, label: str) -> None:
    (loc,) = parse(text).locations
    assert loc.label == label and loc.resolution != "unresolved"


def test_the_two_frankfurts_are_told_apart_by_their_parenthesis() -> None:
    oder, main, bare = (parse(t).locations[0] for t in ("Frankfurt (Oder)", "Frankfurt am Main", "Frankfurt"))
    assert oder.resolution == main.resolution == "unique" and oder.candidates != main.candidates
    assert bare.resolution == "ambiguous"
    assert {c.gazetteer_id for c in bare.candidates} >= {
        oder.candidates[0].gazetteer_id,
        main.candidates[0].gazetteer_id,
    }


# --- markers ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Remote", "remote"),
        ("remote", "remote"),
        ("Homeoffice", "remote"),
        ("Home Office", "remote"),
        ("Home-Office", "remote"),
        ("Remote (Germany)", "remote"),
        ("Remote / Homeoffice", "remote"),
        ("Deutschlandweit", "country_wide"),
        ("Bundesweit", "country_wide"),
        ("Beliebiger Ort", "country_wide"),
        ("Anywhere", "country_wide"),
        ("Remote; Deutschlandweit", "country_wide"),
    ],
)
def test_markers_set_the_kind_and_are_not_locations(text: str, kind: str) -> None:
    p = parse(text)
    assert p.kind == kind and p.locations == [] and not p.positively_non_de


def test_a_marker_next_to_places_does_not_hide_them_or_make_them_foreign() -> None:
    p = parse("Berlin / Remote")
    assert p.kind == "city" and labels("Berlin / Remote") == ["Berlin"]
    z = parse("Zürich / Remote")
    assert z.locations[0].candidates[0].country_code == "CH" and not z.positively_non_de


# --- outside Germany --------------------------------------------------------------------------


def test_foreign_cities_are_unique_candidates_outside_germany() -> None:
    for text, code in [("Zürich", "CH"), ("Wien", "AT"), ("Vienna", "AT"), ("London", "GB"), ("Prag", "CZ")]:
        p = parse(text)
        (loc,) = p.locations
        assert loc.resolution == "unique" and [c.country_code for c in loc.candidates] == [code], text
        assert p.positively_non_de and positively_outside_germany(p.locations), text
        assert -90 < loc.candidates[0].lat < 90 and loc.candidates[0].gazetteer_id.startswith("geonames:")


def test_a_country_name_is_unresolved_but_positively_foreign() -> None:
    for text in ("Schweiz", "Austria", "Österreich", "Switzerland", "USA", "Vereinigtes Königreich"):
        p = parse(text)
        (loc,) = p.locations
        assert loc.resolution == "unresolved" and loc.candidates == [] and p.positively_non_de, text
        assert positively_outside_germany(p.locations) is False, text  # stored locations carry no evidence


def test_positively_foreign_needs_every_part_foreign() -> None:
    assert parse("Zürich / Wien").positively_non_de
    assert parse("Zürich; Schweiz").positively_non_de
    assert not parse("Zürich / Berlin").positively_non_de
    assert not parse("Zürich / Hauptsitz Nord").positively_non_de
    assert not parse("Zürich / Deutschland").positively_non_de
    assert not parse("Berlin").positively_non_de


def test_no_german_place_name_is_ever_positively_foreign() -> None:
    g = Gazetteer.load()
    for place in g.search("", limit=100_000):
        p = parse(place.name)
        assert not p.positively_non_de, place.name
        assert place.gazetteer_id in {c.gazetteer_id for loc in p.locations for c in loc.candidates}, (
            place.name
        )


@pytest.mark.parametrize(
    ("text", "countries_", "foreign"),
    [
        ("Berlin, Germany", [["DE"]], False),
        ("Berlin, Deutschland", [["DE"]], False),
        ("Zürich, Schweiz", [["CH"]], True),
        ("Salzburg, Austria", [["AT"]], True),
        ("Hintertux, Austria", [[]], True),  # unknown town, but the country is named
        ("Kleinkleckersdorf, Deutschland", [[]], False),
        ("Berlin, Austria", [["DE"]], False),  # a country that contradicts the name does not turn it foreign
        ("Berlin, Hamburg", [[]], False),  # a list or a qualifier? Not guessed.
        ("Berlin, Berlin, Germany", [[]], False),
    ],
)
def test_a_trailing_country_selects_candidates(text: str, countries_: list[list[str]], foreign: bool) -> None:
    assert countries(text) == countries_
    assert parse(text).positively_non_de is foreign


def test_a_country_qualifier_chooses_between_homonyms_across_borders() -> None:
    bergen_de, bergen_no = parse("Bergen, Deutschland"), parse("Bergen, Norway")
    assert {c.country_code for c in bergen_de.locations[0].candidates} == {"DE"}
    assert bergen_de.locations[0].resolution == "ambiguous" and not bergen_de.positively_non_de
    assert bergen_no.locations[0].resolution == "unique" and bergen_no.positively_non_de


# --- coordinates ------------------------------------------------------------------------------


def test_coordinates_in_text_are_a_unique_location() -> None:
    p = parse("52.52, 13.405")
    (loc,) = p.locations
    assert loc.resolution == "unique" and loc.candidates[0].country_code == "DE" and not p.positively_non_de
    assert (loc.candidates[0].lat, loc.candidates[0].lon) == (52.52, 13.405)
    assert parse("47.3769, 8.5417").positively_non_de  # Zürich


def test_coordinates_take_the_id_and_country_of_the_closest_known_place() -> None:
    berlin = location_from_coordinates("Berlin", 52.52, 13.405, source="adzuna")
    assert (berlin.label, berlin.resolution, berlin.postal_code, berlin.source) == (
        "Berlin",
        "unique",
        None,
        "adzuna",
    )
    (c,) = berlin.candidates
    assert (
        c.country_code == "DE"
        and c.gazetteer_id.startswith("geonames:")
        and (c.lat, c.lon) == (52.52, 13.405)
    )
    zurich = location_from_coordinates("Zürich", 47.3769, 8.5417, source="x")
    assert [c.country_code for c in zurich.candidates] == ["CH"] and positively_outside_germany([zurich])
    warsaw = location_from_coordinates("Warschau", 52.23, 21.01, source="x")
    assert [c.country_code for c in warsaw.candidates] == ["PL"]


def test_coordinates_far_from_every_known_place_have_no_known_country() -> None:
    atlantic = location_from_coordinates("Atlantik", 30.0, -40.0, source="x")
    assert atlantic.resolution == "unresolved" and atlantic.candidates == []
    assert positively_outside_germany([atlantic]) is False


@pytest.mark.parametrize("lat, lon", [(91.0, 0.0), (0.0, 181.0), (math.nan, 1.0), (1.0, math.inf)])
def test_impossible_coordinates_are_rejected(lat: float, lon: float) -> None:
    with pytest.raises(ValueError, match="not a coordinate pair"):
        location_from_coordinates("x", lat, lon, source="x")


# --- JSearch ----------------------------------------------------------------------------------


def jsearch_bergen(country: str | None) -> Location:
    return parse_jsearch_location(None, "Bergen", country).locations[0]


def test_jsearch_prefers_the_structured_city_and_uses_the_country_for_homonyms() -> None:
    both = parse_jsearch_location("Hamburg • über X", "Berlin", "DE")
    assert [loc.label for loc in both.locations] == ["Berlin"]
    assert [loc.source for loc in both.locations] == ["jsearch"]
    assert jsearch_bergen(None).resolution == "ambiguous"
    assert [c.country_code for c in jsearch_bergen("NO").candidates] == ["NO"] and jsearch_bergen(
        "NO"
    ).resolution == "unique"
    assert {c.country_code for c in jsearch_bergen("de").candidates} == {"DE"}
    assert jsearch_bergen("XX").resolution == "ambiguous"  # a country that matches none changes nothing
    assert parse_jsearch_location(None, "Springfield", "US").locations[0].resolution == "unresolved"


def test_jsearch_location_string_pieces() -> None:
    p = parse_jsearch_location("Berlin • Hamburg • über LinkedIn", None, None)
    assert [loc.label for loc in p.locations] == ["Berlin", "Hamburg"] and p.kind == "multi"
    assert parse_jsearch_location("München • via Indeed", None, None).locations[0].label == "München"
    assert parse_jsearch_location("Anywhere", None, None).kind == "country_wide"
    assert parse_jsearch_location("Remote • über Jooble", None, None).kind == "remote"
    assert parse_jsearch_location("Zürich, Schweiz  •  über Indeed", None, None).positively_non_de


@pytest.mark.parametrize("city", [None, "", "   "])
def test_jsearch_without_a_city_reads_the_location_string_and_with_nothing_is_unknown(
    city: str | None,
) -> None:
    assert parse_jsearch_location("Berlin", city, "DE").locations[0].label == "Berlin"
    nothing = parse_jsearch_location(None, city, "DE")
    assert nothing.locations == [] and nothing.kind == "unknown" and not nothing.positively_non_de
    assert parse_jsearch_location("", None, None).kind == "unknown"


# --- stored locations -------------------------------------------------------------------------


def candidate(country: str) -> Candidate:
    return Candidate(gazetteer_id="geonames:1", country_code=country, lat=1.0, lon=2.0)


def stored(resolution: str, *countries_: str) -> Location:
    return Location.model_validate(
        {
            "label": "x",
            "resolution": resolution,
            "candidates": [candidate(c).model_dump() for c in countries_],
            "postal_code": None,
            "source": "x",
        }
    )


def test_positively_outside_germany_needs_every_location_resolved_and_every_candidate_foreign() -> None:
    assert positively_outside_germany([stored("unique", "CH")]) is True
    assert positively_outside_germany([stored("unique", "CH"), stored("ambiguous", "AT", "FR")]) is True
    assert positively_outside_germany([]) is False
    assert positively_outside_germany([stored("unresolved")]) is False
    assert positively_outside_germany([stored("unique", "CH"), stored("unresolved")]) is False
    assert positively_outside_germany([stored("unique", "CH"), stored("unique", "DE")]) is False
    assert positively_outside_germany([stored("ambiguous", "NO", "DE")]) is False
    assert positively_outside_germany([stored("unique", "")]) is False  # an unknown country is no evidence
    assert positively_outside_germany([stored("unresolved", "CH")]) is False  # the resolution decides


def test_locations_round_trip_through_json() -> None:
    parsed = parse("10115 Berlin; Bergen; Zürich")
    dumped = [loc.model_dump(mode="json") for loc in parsed.locations]
    assert set(dumped[0]) == {"label", "resolution", "candidates", "postal_code", "source"}
    assert set(dumped[0]["candidates"][0]) == {"gazetteer_id", "country_code", "lat", "lon"}
    assert [Location.model_validate(d) for d in dumped] == parsed.locations
