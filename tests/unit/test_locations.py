import math

import pytest
from jobhunter.geo.gazetteer import NON_DE_COLUMNS, PLACE_COLUMNS, Gazetteer
from jobhunter.normalize.text import fold
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
        "geonames:1\tAlpha\talpha\t\tDE\t05\t06411000\t50.0\t8.0\t5000\t11111|22222|33333",
        "geonames:2\tBeta\tbeta\t\tDE\t05\t06432001\t50.05\t8.05\t9000\t11111",
        "geonames:3\tGamma\tgamma\t\tDE\t02\t09162000\t51.0\t9.0\t7000\t22222",
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
        # V17 (Task 6): a city-state named twice, then its country, is the city (Arbeitnow's `<city>, <state>,
        # <country>`); a list of two different places (above; `Berlin, Hamburg, Austria`) is still not guessed
        ("Berlin, Berlin, Germany", [["DE"]], False),
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


@pytest.mark.parametrize(
    "text",
    [
        "Deutschland, Österreich",
        "Germany, Austria",
        "Germany, Switzerland",
        "Deutschland, Schweiz",
        "Deutschland, Österreich, Schweiz",
        "Berlin, Hamburg, Austria",
        "Remote, Austria",
        "Anywhere, Schweiz",
        "Sachsen, Österreich",
    ],
)
def test_a_comma_list_that_names_germany_or_a_place_or_a_marker_is_never_positively_foreign(
    text: str,
) -> None:
    p = parse(text)
    assert not p.positively_non_de, text
    assert all(loc.resolution == "unresolved" for loc in p.locations), (
        text
    )  # nothing is guessed from the tail
    assert positively_outside_germany(p.locations) is False


def test_the_same_countries_in_a_list_with_und_or_slash_are_not_foreign_either() -> None:
    for text in ("Deutschland und Österreich", "Deutschland / Schweiz", "Berlin; Hamburg; Austria"):
        assert not parse(text).positively_non_de, text
    m = parse("Deutschland, Österreich und Schweiz")  # the comma part is unresolved, the last part is foreign
    assert m.kind == "multi" and not m.positively_non_de
    assert [loc.resolution for loc in m.locations] == ["unresolved", "unresolved"]


def test_a_country_qualifier_still_needs_only_a_single_head() -> None:
    assert parse("Hintertux, Austria").positively_non_de and parse("Wien, Österreich").positively_non_de
    assert not parse("Hintertux, Deutschland").positively_non_de
    assert parse("Zürich, Schweiz; Wien, Österreich").positively_non_de


STATES = [
    "Sachsen",
    "Brandenburg",
    "Bayern",
    "NRW",
    "Thüringen",
    "Hessen",
    "Saarland",
    "Niedersachsen",
    "Freistaat Sachsen",
    "Sachsen-Anhalt",
    "Baden-Württemberg",
    "Saxony",
    "Bavaria",
    "Lower Saxony",
    "North Rhine-Westphalia",
]


@pytest.mark.parametrize("text", STATES)
def test_a_federal_state_is_a_region_not_a_place(text: str) -> None:
    p = parse(text)
    (loc,) = p.locations
    assert loc.resolution == "unresolved" and loc.candidates == [] and loc.label == text
    assert not p.positively_non_de
    assert parse(f"{text}, Deutschland").locations[0].candidates == []
    assert parse(f"{text}, Germany").locations[0].resolution == "unresolved"


def test_a_federal_state_next_to_a_city_leaves_only_the_city_resolved() -> None:
    p = parse("Berlin/Brandenburg")
    assert [(loc.label, loc.resolution) for loc in p.locations] == [
        ("Berlin", "unique"),
        ("Brandenburg", "unresolved"),
    ]
    assert [(loc.label, loc.resolution) for loc in parse("Dresden; Sachsen").locations] == [
        ("Dresden", "unique"),
        ("Sachsen", "unresolved"),
    ]
    assert not parse("Zürich / Sachsen").positively_non_de


def test_the_city_states_and_towns_named_like_a_state_remain_places() -> None:
    for text in ("Berlin", "Hamburg", "Bremen", "Brandenburg an der Havel", "Sachsen bei Ansbach"):
        (loc,) = parse(text).locations
        assert loc.resolution in ("unique", "ambiguous") and loc.candidates, text
    assert parse("Sachsen (Dresden)").locations[0].resolution == "unresolved"
    assert parse_jsearch_location(None, "Sachsen", "DE").locations[0].resolution == "unresolved"


# --- coordinates ------------------------------------------------------------------------------


def test_coordinates_in_text_are_a_unique_location() -> None:
    p = parse("52.52, 13.405")
    (loc,) = p.locations
    assert loc.resolution == "unique" and loc.candidates[0].country_code == "DE" and not p.positively_non_de
    assert (loc.candidates[0].lat, loc.candidates[0].lon) == (52.52, 13.405)
    assert parse("47.3769, 8.5417").positively_non_de  # Zürich


def test_coordinates_whose_fraction_looks_like_a_postal_code_are_still_coordinates() -> None:
    munich_code = parse("47.80331, 8.54170")  # "80331" is the postal code of München
    (loc,) = munich_code.locations
    assert loc.postal_code is None and loc.label == "47.80331, 8.54170" and loc.resolution == "unique"
    assert (loc.candidates[0].lat, loc.candidates[0].lon) == (47.80331, 8.5417)
    berlin_code = parse("48.10115, 11.57549")  # "10115" is the postal code of Berlin; the point is in Munich
    (loc,) = berlin_code.locations
    assert loc.postal_code is None and loc.candidates[0].gazetteer_id != "geonames:2950159"
    assert (loc.candidates[0].lat, loc.candidates[0].lon) == (48.10115, 11.57549)
    assert not berlin_code.positively_non_de and loc.candidates[0].country_code == "DE"


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


@pytest.mark.parametrize(
    "text", ["95.10115, 11.5", "99.5, 200.1", "-91.5, 10.0", "50.0, -180.5", "12.5, 999.9"]
)
def test_coordinate_shaped_text_out_of_range_is_an_unresolved_location_not_an_error(text: str) -> None:
    p = parse(text)
    (loc,) = p.locations
    # "95.10115" holds the 5-digit run 10115 (Berlin's postal code): it must not become a postal code either
    assert (loc.label, loc.resolution, loc.candidates, loc.postal_code) == (text, "unresolved", [], None)
    assert p.kind == "city" and not p.positively_non_de


def test_an_out_of_range_coordinate_pair_leaves_the_other_parts_of_the_string_alone() -> None:
    p = parse("Berlin; 99.5, 200.1")
    assert [(loc.label, loc.resolution) for loc in p.locations] == [
        ("Berlin", "unique"),
        ("99.5, 200.1", "unresolved"),
    ]
    assert p.kind == "multi" and not p.positively_non_de
    assert parse_jsearch_location(None, "95.10115, 11.5", "DE").locations[0].resolution == "unresolved"
    assert parse_jsearch_location("99.5, 200.1 • über X", None, None).locations[0].resolution == "unresolved"


@pytest.mark.parametrize("text", ["90.0, 180.0", "-90.0, -180.0", "0.0, 0.0"])
def test_coordinates_at_the_edge_of_the_range_are_still_coordinates(text: str) -> None:
    (loc,) = parse(text).locations
    assert loc.label == text and loc.postal_code is None


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


# V17 (Task 6): the forms of the recorded fixtures that the parser left unresolved. A comma qualifies a name
# only when the gazetteer says so (a district near its city, a city in its federal state, a city under its own
# name as state); a list of places stays unresolved, and nothing becomes foreign that names Germany.
@pytest.mark.parametrize(
    ("text", "admin1"),
    [
        ("Kreuzberg, Berlin", "16"),  # Adzuna: <district>, <city>
        ("Mariendorf, Berlin", "16"),
        ("Potsdam, Brandenburg", "11"),  # <city>, <federal state>, in German or English
        ("Munich, Bavaria", "02"),
        ("Eschborn, Hesse", "05"),
        ("Düsseldorf, North Rhine-Westphalia", "07"),
        ("Detmold, Nordrhein-Westfalen, Deutschland", "07"),  # Arbeitnow: <city>, <state>, <country>
        ("München, Bayern, Deutschland", "02"),
        ("Munich, Bayern, Germany", "02"),
        ("Berlin, Berlin, Deutschland", "16"),  # a city-state under its own name
        ("Hamburg, Hamburg, Duitsland", "04"),  # Germany in Dutch
    ],
)
def test_a_qualified_german_place_resolves_in_its_area(text: str, admin1: str) -> None:
    p = parse(text)
    (loc,) = p.locations
    assert loc.resolution == "unique" and not p.positively_non_de, text
    (candidate,) = loc.candidates
    place = next(
        pl
        for pl in Gazetteer.load().by_name(candidate_name(text))
        if pl.gazetteer_id == candidate.gazetteer_id
    )
    assert place.admin1 == admin1, text


def candidate_name(text: str) -> str:
    from jobhunter.normalize.text import fold

    head = text.split(",")[0].strip()
    return fold({"munich": "münchen"}.get(fold(head), head))


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Amsterdam, Noord-Holland, Netherlands", "NL"),
        ("Barcelona, , Spain", "ES"),
        ("Milan, Lombardy, Italy", "IT"),
        ("Sofia, Sofia (stolitsa), Bulgaria", "BG"),
    ],
)
def test_a_qualified_foreign_city_resolves_in_its_country(text: str, code: str) -> None:
    p = parse(text)
    (loc,) = p.locations
    assert [c.country_code for c in loc.candidates] == [code] and p.positively_non_de, text


@pytest.mark.parametrize(
    "text",
    [
        "Berlin, Hamburg",
        "Berlin, Hamburg, Germany",
        "Berlin, Hamburg, Austria",
        "Deutschland, Österreich, Schweiz",
    ],
)
def test_a_comma_list_of_places_stays_unresolved(text: str) -> None:
    p = parse(text)
    assert all(loc.resolution == "unresolved" for loc in p.locations) and not p.positively_non_de, text


def test_an_office_suffix_and_a_remote_job_marker() -> None:
    assert countries("Berlin Office") == [["DE"]] and countries("London Office") == [["GB"]]
    assert parse("Remote job").kind == "remote" and parse("Remote jobs").locations == []


@pytest.mark.parametrize(
    ("text", "foreign"),
    [
        ("Germany", False),
        ("Deutschland", False),
        ("Norway", True),
        ("Bosnia", True),
        ("Czech Republic", True),
    ],
)
def test_a_bare_country_name_is_country_wide(text: str, foreign: bool) -> None:
    p = parse(text)
    assert p.kind == "country_wide" and p.positively_non_de is foreign, text
    (loc,) = p.locations  # the country stays a (unresolved) location, as before
    assert loc.resolution == "unresolved"


def test_a_foreign_qualifier_never_moves_a_city_to_another_foreign_country() -> None:
    # V17: SmartRecruiters' `Santa Cruz, , Bolivia` is no Spanish Santa Cruz; a German name stays German
    p = parse("Santa Cruz, , Bolivia")
    assert [loc.resolution for loc in p.locations] == ["unresolved"] and p.positively_non_de
    assert countries("Santa Cruz, Bolivia") == [[]] and countries("Berlin, Austria") == [["DE"]]


@pytest.mark.parametrize(
    "text",
    [
        "Köln, Bonn",
        "Berlin, Potsdam",
        "Nürnberg, Fürth",
        "Mannheim, Heidelberg",
        "Düsseldorf, Neuss",
        # ruling T6-c: in the reversed order the smaller head is no district either, for it is not in the
        # tail city's municipality
        "Bonn, Köln",
        "Potsdam, Berlin",
        "Offenbach, Frankfurt",
        "Offenbach am Main, Frankfurt am Main",
        "Fürth, Nürnberg",
        "Neuss, Düsseldorf",
    ],
)
def test_a_list_of_neighbouring_cities_stays_unresolved(text: str) -> None:
    # 6a review, finding 5: `<district>, <city>` needs the head to be smaller than the city it names (§8.12:
    # positive evidence only); two cities near each other are a list, not a district and its city
    p = parse(text)
    assert [loc.resolution for loc in p.locations] == ["unresolved"] and not p.positively_non_de, text


@pytest.mark.parametrize(
    ("text", "city"),
    [
        ("Kreuzberg, Berlin", "berlin"),
        ("Adlershof, Berlin", "berlin"),
        ("Friedrichshain, Berlin", "berlin"),
        ("Wedding, Berlin", "berlin"),
        ("Altona, Hamburg", "hamburg"),
        ("Zwötzen, Gera", "gera"),  # an Ortsteil that GeoNames lists as a populated place of its own
    ],
)
def test_a_district_resolves_in_the_municipality_of_its_city(text: str, city: str) -> None:
    # ruling T6-c: the head is a true district, a place in the tail city's municipality
    (loc,) = parse(text).locations
    assert loc.resolution == "unique", text
    g = Gazetteer.load()
    (district,) = [
        p for p in g.by_name(fold(text.split(",")[0])) if p.gazetteer_id == loc.candidates[0].gazetteer_id
    ]
    assert district.municipality in {p.municipality for p in g.by_name(city)}, text


@pytest.mark.parametrize(("key", "resolution"), [("", "unresolved"), ("06412000", "unique")])
def test_an_empty_municipality_key_is_no_shared_municipality(
    monkeypatch: pytest.MonkeyPatch, key: str, resolution: str
) -> None:
    # F1 review, Minor 4: two neighbouring places without a key are no district and its city
    rows = [
        f"geonames:1\tKleinort\tkleinort\t\tDE\t05\t{key}\t50.0\t8.0\t2000\t",
        f"geonames:2\tGrossstadt\tgrossstadt\t\tDE\t05\t{key}\t50.02\t8.02\t90000\t",
    ]
    g = Gazetteer.from_tsv("\n".join(["\t".join(PLACE_COLUMNS), *rows, ""]), "\t".join(NON_DE_COLUMNS) + "\n")
    monkeypatch.setattr(Gazetteer, "load", classmethod(lambda cls: g))
    assert [loc.resolution for loc in parse("Kleinort, Grossstadt").locations] == [resolution]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Remote, Germany", "remote"),
        ("Germany (Remote)", "remote"),
        ("Remote (Deutschland)", "remote"),
        ("Deutschland, remote", "remote"),
        ("Homeoffice, Deutschland", "remote"),
        ("Remote - Germany", "remote"),
        ("Deutschlandweit (Germany)", "country_wide"),
    ],
)
def test_a_marker_qualified_by_germany_is_the_marker(text: str, kind: str) -> None:
    # fix wave F1 (ledger L48): not one unresolved `city` label; the same as `Remote (Germany)`
    p = parse(text)
    assert (p.kind, p.locations, p.positively_non_de) == (kind, [], False), text


def test_a_marker_with_another_country_or_place_is_not_only_a_marker() -> None:
    assert (
        parse("Remote, Austria").kind == "city"
    )  # a foreign remote stays an unresolved label (T4, as before)
    assert labels("Germany (Remote) / Berlin") == ["Berlin"]  # a marker part beside a place
