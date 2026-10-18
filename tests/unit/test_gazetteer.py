import re

import pytest
from jobhunter.geo.gazetteer import NON_DE_COLUMNS, PLACE_COLUMNS, Gazetteer, NonDePlace, Place, distance_km
from jobhunter.normalize.text import fold


def test_lookups_and_sizes() -> None:
    g = Gazetteer.load()
    berlin = g.by_name("berlin")
    assert len(berlin) == 1 and berlin[0].country_code == "DE" and berlin[0].population > 3_000_000
    assert [p.name for p in g.by_postal_code("10115")] == ["Berlin"] and len(g.by_name("neustadt")) > 3
    # am Main (Hessen, GeoNames admin1 05) and (Oder) (Brandenburg, admin1 11; 12 is Mecklenburg-Vorpommern)
    assert {p.admin1 for p in g.by_name("frankfurt")} >= {"05", "11"}
    assert g.by_name("monchengladbach") == g.by_name("moenchengladbach")
    assert g.by_name("monchengladbach")[0].name == "Mönchengladbach"
    zurich, schweiz, london = g.non_de("zurich"), g.non_de("schweiz"), g.non_de("london")
    assert zurich is not None and zurich.country_code == "CH"
    assert schweiz is not None and schweiz.kind == "country"
    assert london is not None and london.kind == "city"
    # 7,662 places of 1,000 or more inhabitants in the GeoNames dump of 2026-10-18
    assert g.non_de("berlin") is None and 7_000 <= sum(1 for _ in g.search("", limit=100_000)) <= 20_000
    assert 3.0 < distance_km(52.52, 13.405, 52.5, 13.35) < 5.0


def test_load_is_cached_and_reads_the_packaged_files() -> None:
    assert Gazetteer.load() is Gazetteer.load()


def everything(g: Gazetteer) -> list[Place]:
    return g.search("", limit=100_000)


def test_packaged_places_are_well_formed_and_reachable_by_every_key() -> None:
    g = Gazetteer.load()
    places = everything(g)
    assert len({p.gazetteer_id for p in places}) == len(places)
    for p in places:
        assert re.fullmatch(r"geonames:\d+", p.gazetteer_id)
        assert p.country_code == "DE" and p.population >= 1_000
        assert p.folded == fold(p.name) and p.folded
        assert re.fullmatch(r"(0[1-9]|1[0-6])", p.admin1), p
        assert 47.0 < p.lat < 55.2 and 5.5 < p.lon < 15.5, p
        assert all(a and a == fold(a) and a != p.folded and "|" not in a for a in p.alt_folded), p
        assert all(re.fullmatch(r"\d{5}", c) for c in p.postal_codes), p
        assert p in g.by_name(p.folded) and all(p in g.by_name(a) for a in p.alt_folded)
        assert all(p in g.by_postal_code(c) for c in p.postal_codes)


def test_search_orders_by_population_and_postal_codes_round_trip() -> None:
    g = Gazetteer.load()
    places = everything(g)
    assert [p.population for p in places] == sorted((p.population for p in places), reverse=True)
    assert places[0].name == "Berlin"
    codes = {c for p in places for c in p.postal_codes}
    assert len(codes) > 10_000  # almost every one of the 10,815 postal codes of the file maps to a place
    assert all(g.by_postal_code(c) for c in codes) and g.by_postal_code("00000") == []


def test_umlaut_spellings_and_german_display_names() -> None:
    g = Gazetteer.load()
    assert g.by_name("koeln") == g.by_name("koln") and [p.name for p in g.by_name("koln")] == ["Köln"]
    munich = g.by_name("munchen")
    assert [p.name for p in munich] == ["München"]  # GeoNames' own main name is the English "Munich"
    assert g.by_name("muenchen") == munich == g.by_name("munich")
    assert [p.name for p in g.by_name("nurnberg")] == ["Nürnberg"] and g.by_name("nuremberg") == g.by_name(
        "nurnberg"
    )
    assert [
        p.name for p in g.by_name("strasse")
    ] == []  # the transliteration is of the name, not a free-for-all


def test_homonyms_all_stay_candidates() -> None:
    g = Gazetteer.load()
    neustadt = g.by_name("neustadt")
    assert len({p.gazetteer_id for p in neustadt}) == len(neustadt) >= 4
    assert len({p.admin1 for p in neustadt}) >= 3
    assert {p.name for p in g.by_name("frankfurt")} >= {"Frankfurt am Main", "Frankfurt (Oder)"}
    assert {p.name for p in g.by_name("frankfurt/oder")} == {"Frankfurt (Oder)"}


def test_postal_codes_map_to_the_town_they_name() -> None:
    g = Gazetteer.load()
    assert [p.name for p in g.by_postal_code("80331")] == ["München"]
    assert [p.name for p in g.by_postal_code("60311")] == ["Frankfurt am Main"]
    assert [p.name for p in g.by_postal_code("15230")] == ["Frankfurt (Oder)"]
    # folding makes Fürth and Furth one name; the exact spelling of the postal row tells them apart
    assert [p.name for p in g.by_postal_code("90762")] == ["Fürth"]
    assert len(g.by_name("berlin")[0].postal_codes) > 100


def test_prefix_search_over_names_alternate_spellings_and_postal_codes() -> None:
    g = Gazetteer.load()
    assert [p.name for p in g.search("frankfurt", limit=5)][:2] == ["Frankfurt am Main", "Frankfurt (Oder)"]
    assert g.search("Mün", limit=3)[0].name == "München"  # the query is folded
    assert [p.name for p in g.search("MUENCHEN", limit=1)] == ["München"]  # an alternate spelling
    assert g.search("ber", limit=1)[0].name == "Berlin"
    by_code = g.search("1011", limit=100)
    assert by_code[0].name == "Berlin" and all(
        any(c.startswith("1011") for c in p.postal_codes) for p in by_code
    )
    assert len(g.search("frankfurt", limit=1)) == 1 and g.search("frankfurt", limit=0) == []
    assert g.search("zzzzzz") == [] and len(g.search("", limit=7)) == 7


def test_non_german_cities_and_countries() -> None:
    g = Gazetteer.load()
    vienna = g.non_de("wien")  # the German name is one of the city's lookup rows
    assert vienna is not None and (vienna.country_code, vienna.kind, vienna.name) == ("AT", "city", "Vienna")
    assert vienna.gazetteer_id.startswith("geonames:") and vienna.lat is not None and vienna.lon is not None
    for german, english in [("wien", "vienna"), ("prag", "praha"), ("mailand", "milan")]:
        a, b = g.non_de(german), g.non_de(english)
        assert a is not None and b is not None and a.gazetteer_id == b.gazetteer_id != "", german
    for key, code in [
        ("osterreich", "AT"),
        ("austria", "AT"),
        ("schweiz", "CH"),
        ("usa", "US"),
        ("uk", "GB"),
    ]:
        country = g.non_de(key)
        assert country is not None and (country.kind, country.country_code) == ("country", code), key
        assert country.gazetteer_id == "" and country.lat is None and country.lon is None
    assert g.non_de("deutschland") is None and g.non_de("germany") is None  # Germany is not "non-German"
    assert g.non_de("hamburg") is None and g.non_de("") is None
    countries = {r.country_code for r in g._non_de.values() if r.kind == "country"}
    assert len(countries) == 251  # every country of countryInfo.txt but Germany


def test_german_place_names() -> None:
    g = Gazetteer.load()
    assert all(
        g.is_german_place_name(k) for k in ("berlin", "neustadt", "moenchengladbach", "frankfurt/oder")
    )
    assert not any(g.is_german_place_name(k) for k in ("zurich", "wien", "", "hauptsitz nord", "remote"))


def test_nearest_place_of_either_kind() -> None:
    g = Gazetteer.load()
    stuttgart = g.by_name("stuttgart")[0]
    assert g.nearest(stuttgart.lat, stuttgart.lon) == stuttgart
    zurich = g.nearest(47.3769, 8.5417)
    assert isinstance(zurich, NonDePlace) and zurich.country_code == "CH"
    assert g.nearest(30.0, -40.0) is None  # the Atlantic: nothing within 50 km
    assert g.nearest(stuttgart.lat + 0.3, stuttgart.lon, max_km=1.0) is None  # 33 km from the closest place


def test_distance_km() -> None:
    assert distance_km(52.52, 13.405, 52.52, 13.405) == 0.0
    berlin_munich = distance_km(52.52, 13.405, 48.137, 11.575)
    assert 500 < berlin_munich < 510 and berlin_munich == distance_km(48.137, 11.575, 52.52, 13.405)
    assert 20_000 < distance_km(0, 0, 0, 180) < 20_050  # half the Earth's circumference, no domain error


# --- the loader, on small files -------------------------------------------------------------------

PLACES_HEADER = "\t".join(PLACE_COLUMNS)
NON_DE_HEADER = "\t".join(NON_DE_COLUMNS)
SMALL_PLACES = "\n".join(
    [
        PLACES_HEADER,
        "geonames:1\tAlt\talt\talten|altstadt\tDE\t05\t50.0\t8.0\t5000\t11111|22222",
        "geonames:2\tNeu\tneu\talt\tDE\t05\t50.05\t8.05\t9000\t11111",
        "geonames:3\tFern\tfern\t\tDE\t02\t49.0\t11.0\t1200\t",
        "",
    ]
)
SMALL_NON_DE = "\n".join(
    [
        NON_DE_HEADER,
        "Vienna\twien\tAT\tcity\tgeonames:9\t48.2\t16.4",
        "Austria\taustria\tAT\tcountry\t\t\t",
        "",
    ]
)


def test_loader_on_a_small_gazetteer() -> None:
    g = Gazetteer.from_tsv(SMALL_PLACES, SMALL_NON_DE)
    # "alt" is the name of Alt and a spelling of Neu; the more populous Neu comes first
    assert [p.name for p in g.by_name("alt")] == ["Neu", "Alt"]
    assert [p.name for p in g.by_name("altstadt")] == ["Alt"] and g.by_name("nope") == []
    assert [p.name for p in g.by_postal_code("11111")] == ["Neu", "Alt"]
    assert g.by_postal_code("22222")[0].postal_codes == ("11111", "22222")
    assert [p.name for p in g.search("")] == ["Neu", "Alt", "Fern"]
    # a match on the name or a postal code ranks above a match on another spelling, whatever the population
    assert [p.name for p in g.search("alt")] == ["Alt", "Neu"]
    assert [p.name for p in g.search("2222")] == ["Alt"]
    assert g.is_german_place_name("alt") and not g.is_german_place_name("wien")
    assert g.non_de("wien") == NonDePlace("Vienna", "wien", "AT", "city", "geonames:9", 48.2, 16.4)
    assert g.non_de("austria") == NonDePlace("Austria", "austria", "AT", "country")
    near_vienna = g.nearest(48.21, 16.41)
    assert isinstance(near_vienna, NonDePlace) and near_vienna.name == "Vienna"
    fern = g.by_name("fern")[0]
    assert fern.alt_folded == () and fern.postal_codes == () and g.nearest(49.01, 11.01) == fern


@pytest.mark.parametrize(
    ("de", "non_de", "message"),
    [
        ("name\tfolded\n", NON_DE_HEADER + "\n", "de_places: unexpected header"),
        (PLACES_HEADER + "\n", "name\n", "non_de_places: unexpected header"),
        (
            PLACES_HEADER + "\ngeonames:1\tAlt\n",
            NON_DE_HEADER + "\n",
            "de_places line 2: 2 cells, expected 10",
        ),
        (PLACES_HEADER + "\n", NON_DE_HEADER + "\nx\ty\tAT\tvillage\t\t\t\n", "unknown kind 'village'"),
        (
            PLACES_HEADER + "\n",
            NON_DE_HEADER + "\nA\ta\tAT\tcountry\t\t\t\nB\ta\tCH\tcountry\t\t\t\n",
            "duplicate key 'a'",
        ),
    ],
)
def test_loader_rejects_malformed_files(de: str, non_de: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Gazetteer.from_tsv(de, non_de)
