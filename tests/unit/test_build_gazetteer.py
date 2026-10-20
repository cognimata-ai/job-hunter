"""scripts/build_gazetteer.py (Task 4): the rules that turn the GeoNames files into the packaged TSVs.

The inputs are small synthetic files in the GeoNames layouts, so nothing is downloaded; the committed TSVs are
checked by test_gazetteer.py.
"""

import importlib.util
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from jobhunter.geo.gazetteer import Gazetteer, Place

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/build_gazetteer.py"
DATA = REPO / "backend/src/jobhunter/geo/data"
MUNICH, NUREMBERG = "2867714", "2861650"


@pytest.fixture(scope="module")
def build() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_gazetteer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def geonames_row(
    geoname_id: str,
    name: str,
    *,
    alt: str = "",
    lat: float = 50.0,
    lon: float = 8.0,
    fclass: str = "P",
    country: str = "DE",
    admin1: str = "05",
    municipality: str = "06412000",
    population: int = 5_000,
) -> str:
    """One row of a GeoNames `dump` file (19 tab-separated cells); `municipality` is the admin4 code."""
    cells = [
        geoname_id,
        name,
        name,
        alt,
        str(lat),
        str(lon),
        fclass,
        "PPL",
        country,
        "",
        admin1,
        "",
        "",
        municipality,
    ]
    return "\t".join([*cells, str(population), "", "", "Europe/Berlin", "2020-01-01"])


def dump(*rows: str, overrides: bool = True) -> str:
    required = [
        geonames_row(MUNICH, "Munich", alt="München,Munchen,Muenchen,MUC,Mjunkhen", admin1="02"),
        geonames_row(NUREMBERG, "Nuremberg", alt="Nürnberg,Nurnberg", admin1="02"),
    ]
    return "\n".join([*rows, *(required if overrides else [])]) + "\n"


def postal(
    code: str, place: str, state_code: str, lat: float, lon: float, accuracy: str = "4", country: str = "DE"
) -> str:
    """One row of the `zip` file (12 tab-separated cells)."""
    return "\t".join(
        [country, code, place, "State", state_code, "", "", "", "", str(lat), str(lon), accuracy]
    )


def place(
    build: ModuleType, geoname_id: str, name: str, lat: float, lon: float, admin1: str, pop: int = 5_000
) -> Place:
    (made,) = build.de_places(
        dump(geonames_row(geoname_id, name, lat=lat, lon=lon, admin1=admin1, population=pop))
    )[:1]
    return made  # type: ignore[no-any-return]


# --- names ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "folded"),
    [
        ("Wien", "wien"),
        ("Köln", "koln"),
        ("ZÜRICH", "zurich"),
        ("Frankfurt/Oder", "frankfurt/oder"),
        ("Halle (Saale)", "halle (saale)"),
        ("Saint-Étienne", "saint-etienne"),
        ("Freiburg i. Br.", "freiburg i. br."),
        ("L\N{RIGHT SINGLE QUOTATION MARK}Aquila", "l'aquila"),
    ],
)
def test_usable_aliases_are_folded_latin_names(build: ModuleType, raw: str, folded: str) -> None:
    assert build.usable_alias(raw) == folded


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "  ",
        "BER",
        "MUC",
        "berurin",
        "bai lin",
        "Берлин",
        "柏林",
        "برلين",
        "12345",
        "'s-Hertogenbosch",
        "-Foo",
        "Bad|Name",
    ],
)
def test_codes_romanisations_and_other_scripts_are_not_aliases(build: ModuleType, raw: str) -> None:
    assert build.usable_alias(raw) is None


def test_umlaut_transliteration_differs_from_folding(build: ModuleType) -> None:
    assert build.umlaut_transliteration("Mönchengladbach, Übach, Straße, Ärzte, Öde") == (
        "Moenchengladbach, Uebach, Strasse, Aerzte, Oede"
    )
    assert build.aliases("Köln,Cologne,KÖLN,BER,berurin,Cologne", "Koeln") == ["cologne", "koeln", "koln"]


# --- German places ----------------------------------------------------------------------------


def test_german_places_are_populated_places_of_at_least_1000_inhabitants(build: ModuleType) -> None:
    text = dump(
        geonames_row("1", "Berlin", population=3_400_000),
        geonames_row("2", "Grenzfall", population=1_000),
        geonames_row("3", "Zu Klein", population=999),
        geonames_row("4", "Ohne Zahl", population=0),
        geonames_row("5", "Landkreis", fclass="A", population=90_000),
        geonames_row("6", "Wien", country="AT", population=1_700_000),
    )
    places = {p.name: p for p in build.de_places(text)}
    assert set(places) == {"Berlin", "Grenzfall", "München", "Nürnberg"}
    assert places["Grenzfall"].gazetteer_id == "geonames:2" and places["Grenzfall"].country_code == "DE"
    assert places["Berlin"].population == 3_400_000 and places["Berlin"].postal_codes == ()


def test_a_place_keeps_its_municipality_key(build: ModuleType) -> None:
    # fix wave F1 (ruling T6-c): the GeoNames admin4 code, which a city and its districts share
    text = dump(
        geonames_row("1", "Berlin", admin1="16", municipality="11000000", population=3_400_000),
        geonames_row("2", "Kreuzberg", admin1="16", municipality="11000000", population=150_000),
        geonames_row("3", "Potsdam", admin1="12", municipality="12054000", population=180_000),
    )
    places = {p.name: p for p in build.de_places(text)}
    assert places["Berlin"].municipality == places["Kreuzberg"].municipality == "11000000"
    assert places["Potsdam"].municipality == "12054000"
    g = Gazetteer.from_tsv(build.render_de_places(places.values()), build.render_non_de_places([]))
    assert [p.municipality for p in g.by_name("kreuzberg")] == ["11000000"]


def test_alt_folded_holds_alternate_names_and_the_umlaut_transliteration(build: ModuleType) -> None:
    text = dump(
        geonames_row(
            "7", "Mönchengladbach", alt="Monchengladbach,MGL,Muenchen-Gladbach,Менхенгладбах,mnsngldbk"
        )
    )
    places = {p.name: p for p in build.de_places(text)}
    mg = places["Mönchengladbach"]
    assert mg.folded == "monchengladbach"
    # the transliteration is added, the plain fold is the name itself, codes and other scripts are gone
    assert mg.alt_folded == ("moenchengladbach", "muenchen-gladbach")


def test_munich_and_nuremberg_are_shown_under_their_german_names(build: ModuleType) -> None:
    places = {p.gazetteer_id: p for p in build.de_places(dump())}
    munich, nuremberg = places[f"geonames:{MUNICH}"], places[f"geonames:{NUREMBERG}"]
    assert (munich.name, munich.folded) == ("München", "munchen")
    assert munich.alt_folded == ("mjunkhen", "muenchen", "munich")  # the GeoNames name stays findable
    assert (nuremberg.name, nuremberg.folded) == (
        "Nürnberg",
        "nurnberg",
    ) and "nuremberg" in nuremberg.alt_folded


def test_the_build_fails_when_an_override_city_is_missing(build: ModuleType) -> None:
    with pytest.raises(ValueError, match="name overrides not found"):
        build.de_places(dump(geonames_row("1", "Berlin"), overrides=False))


def test_geonames_rows_reject_a_row_of_the_wrong_width(build: ModuleType) -> None:
    assert build.geonames_rows("# comment\n\na\tb\n", 2) == [["a", "b"]]
    with pytest.raises(ValueError, match="row 2: 3 cells, expected 2"):
        build.geonames_rows("a\tb\na\tb\tc\n", 2)


# --- postal codes -----------------------------------------------------------------------------


def test_postal_rows_normalise_the_state_and_skip_what_is_not_a_german_code(build: ModuleType) -> None:
    text = "\n".join(
        [
            postal("90762", "Fürth", "BY", 49.47, 10.99),  # ISO letters
            postal("60311", "Frankfurt", "05", 50.1, 8.68),  # GeoNames number
            postal("87491", "Jungholz", "", 47.57, 10.44, accuracy=""),  # no state
            postal("1234", "Kurz", "BY", 49.0, 10.0),
            postal("12345", "Fremd", "BY", 49.0, 10.0, country="AT"),
            postal("10863", "Gericht", "BE", 52.5, 13.4, accuracy=""),
        ]
    )
    rows = build.postal_rows(text)
    assert [(r.code, r.admin1, r.institution) for r in rows] == [
        ("90762", "02", False),
        ("60311", "05", False),
        ("87491", None, True),
        ("10863", "16", True),
    ]


def test_postal_codes_map_by_name_then_by_distance(build: ModuleType) -> None:
    places = [
        place(build, "10", "Alpha", 50.0, 8.0, "05"),
        place(build, "11", "Beta", 50.05, 8.05, "05"),  # 6.8 km from Alpha
        place(build, "12", "Fürth", 49.47, 10.99, "02", pop=100_000),
        place(build, "13", "Furth", 49.50, 11.00, "02"),  # folds to the same name as Fürth, 3 km away
        place(build, "14", "Amberg", 49.44, 11.86, "02"),
        place(build, "15", "Amberg", 48.07, 10.68, "02"),
    ]
    rows = build.postal_rows(
        "\n".join(
            [
                postal("90762", "Fürth", "BY", 49.47, 10.99),
                postal("84000", "Furth", "BY", 49.50, 11.00),
                postal("92224", "Amberg", "BY", 49.44, 11.86),
                postal("11111", "Alpha", "HE", 50.0, 8.0),
                postal("11111", "Nirgends", "HE", 50.05, 8.05),  # another row of a named code adds nothing
                postal("22222", "Dorf", "HE", 50.001, 8.001),  # names no place: the closest within 10 km
                postal("33333", "Weitab", "HE", 53.0, 9.0),  # nothing within 10 km
                postal("44444", "Alpha", "BY", 50.05, 8.05),  # the right name in the wrong state: by distance
                postal("55555", "Dorf", "HE", 50.05, 8.05),
                postal(
                    "55555", "Großkunde GmbH", "HE", 51.5, 9.5, accuracy=""
                ),  # institutions far away, together
                postal("55555", "Verband e.V.", "HE", 51.501, 9.501, accuracy=""),
                postal("66666", "Institut A", "HE", 50.0, 8.0, accuracy=""),
                postal("66666", "Institut B", "HE", 50.002, 8.002, accuracy=""),
                postal(
                    "66666", "Institut C", "HE", 51.5, 9.5, accuracy=""
                ),  # an outlier, the middle row decides
            ]
        )
    )
    made, stats = build.attach_postal_codes(places, rows)
    codes = {p.name + p.gazetteer_id[-2:]: p.postal_codes for p in made}
    assert codes == {
        "Alpha10": ("11111", "22222", "66666"),
        "Beta11": ("44444", "55555"),
        "Fürth12": ("90762",),  # not also Furth, which folds the same
        "Furth13": ("84000",),
        "Amberg14": ("92224",),  # not also the Amberg 160 km away
        "Amberg15": (),
    }
    assert stats == Counter({"by_name": 4, "nearest": 4, "unmapped": 1})


# --- outside Germany --------------------------------------------------------------------------


def country_info(*rows: tuple[str, str, str]) -> str:
    """`countryInfo.txt`: a comment, then (ISO code, name, continent) as 19-cell rows."""
    lines = ["#ISO\tISO3\tnumeric\tfips\tCountry"]
    for code, name, continent in rows:
        cells = [code, code + "X", "000", code, name, "Capital", "1", "1", continent, ".x", "EUR", "Euro"]
        lines.append("\t".join([*cells, "1", "", "", "de", "1", "", ""]))
    return "\n".join(lines) + "\n"


COUNTRIES = country_info(
    ("DE", "Germany", "EU"),
    ("AT", "Austria", "EU"),
    ("LU", "Luxembourg", "EU"),
    ("JP", "Japan", "AS"),
)
GERMAN_NAMES = "# comment\nAT\tÖsterreich\nLU\tLuxemburg\nJP\tJapan\nJP\tNippon\n"


def city(geoname_id: str, name: str, country: str, population: int, *, alt: str = "") -> str:
    return geonames_row(geoname_id, name, alt=alt, country=country, population=population)


def test_non_german_places_are_european_cities_over_100000_and_every_country(build: ModuleType) -> None:
    cities = "\n".join(
        [
            city("1", "Vienna", "AT", 1_700_000, alt="Wien,Vienne,BEC,Беч,wien,Austria"),
            city("2", "Salzburg", "AT", 150_000),
            city("3", "Kleinstadt", "AT", 100_000),  # "over 100,000" is strictly more
            city("4", "Tokyo", "JP", 9_000_000),  # not in Europe
            city("5", "Berlin", "DE", 3_400_000),  # Germany is not "outside"
            city("6", "Luxembourg", "LU", 120_000),
        ]
    )
    rows = {r.folded: r for r in build.non_de_places(cities + "\n", COUNTRIES, GERMAN_NAMES)}
    wien = rows["wien"]
    assert (wien.name, wien.country_code, wien.kind, wien.gazetteer_id) == (
        "Vienna",
        "AT",
        "city",
        "geonames:1",
    )
    assert rows["vienna"].gazetteer_id == rows["vienne"].gazetteer_id == "geonames:1" and "bec" not in rows
    assert "salzburg" in rows and "kleinstadt" not in rows and "tokyo" not in rows and "berlin" not in rows
    assert (rows["osterreich"].kind, rows["osterreich"].country_code, rows["osterreich"].gazetteer_id) == (
        "country",
        "AT",
        "",
    )
    assert rows["japan"].kind == rows["nippon"].kind == "country" and rows["japan"].lat is None
    assert "deutschland" not in rows and "germany" not in rows
    # a city's main name wins over a country name, a country name over a city's alternate name
    assert rows["luxembourg"].kind == "city" and rows["luxemburg"].kind == "country"
    assert rows["austria"].kind == "country"
    assert all(r.lat is not None and r.lon is not None for r in rows.values() if r.kind == "city")


def test_the_more_populous_city_keeps_a_shared_key_and_a_main_name_beats_an_alias(build: ModuleType) -> None:
    cities = "\n".join(
        [
            city("1", "Brest", "AT", 140_000),
            city("2", "Brest", "LU", 340_000),
            city("3", "Foo", "AT", 200_000),
            city("4", "Bar", "AT", 900_000, alt="Foo"),
        ]
    )
    rows = {r.folded: r for r in build.non_de_places(cities + "\n", COUNTRIES, GERMAN_NAMES)}
    assert rows["brest"].country_code == "LU" and rows["foo"].gazetteer_id == "geonames:3"


def test_the_german_country_names_must_cover_the_countries_exactly(build: ModuleType) -> None:
    with pytest.raises(ValueError, match=r"no German name for: \['JP'\]"):
        build.non_de_places("", COUNTRIES, "AT\tÖsterreich\nLU\tLuxemburg\n")
    with pytest.raises(ValueError, match=r"unknown or German country codes: \['DE'\]"):
        build.non_de_places("", COUNTRIES, GERMAN_NAMES + "DE\tDeutschland\n")
    with pytest.raises(ValueError, match="names two countries"):
        build.non_de_places("", COUNTRIES, GERMAN_NAMES + "LU\tÖsterreich\n")
    with pytest.raises(ValueError, match="expected '<code> TAB <name>'"):
        build.german_country_names("AT Österreich\n")


def test_the_committed_german_country_names_are_well_formed(build: ModuleType) -> None:
    names = build.german_country_names((DATA / "country_names_de.tsv").read_text(encoding="utf-8"))
    assert len(names) == 251 and "DE" not in names and all(re.fullmatch(r"[A-Z]{2}", c) for c in names)
    assert (
        names["AT"] == ["Österreich"]
        and names["CH"] == ["Schweiz"]
        and "USA" in names["US"]
        and "UK" in names["GB"]
    )


# --- output -----------------------------------------------------------------------------------


def test_rendered_tsvs_load_back_into_the_same_gazetteer(build: ModuleType) -> None:
    places, _ = build.attach_postal_codes(
        build.de_places(dump(geonames_row("1", "Alpha", alt="Altona,Alpha-Stadt"))),
        build.postal_rows(postal("11111", "Alpha", "HE", 50.0, 8.0)),
    )
    non_de = build.non_de_places(
        city("9", "Vienna", "AT", 1_700_000, alt="Wien") + "\n", COUNTRIES, GERMAN_NAMES
    )
    g = Gazetteer.from_tsv(build.render_de_places(places), build.render_non_de_places(non_de))
    assert g.search("", limit=100) == sorted(places, key=lambda p: (-p.population, p.name, p.gazetteer_id))
    alpha = g.by_name("alpha")[0]
    assert alpha.alt_folded == ("alpha-stadt", "altona") and alpha.postal_codes == ("11111",)
    assert g.by_postal_code("11111") == [alpha] and g.by_name("altona") == [alpha]
    assert g.non_de("wien") == next(r for r in non_de if r.folded == "wien")
    assert g.non_de("osterreich") == next(r for r in non_de if r.folded == "osterreich")


def test_a_cell_with_a_tab_or_newline_cannot_be_written(build: ModuleType) -> None:
    bad: Iterable[Any] = [Place("geonames:1", "A\tB", "a b", (), "DE", "05", "06412000", 1.0, 2.0, 1000, ())]
    with pytest.raises(ValueError, match="holds a tab or newline"):
        build.render_de_places(bad)


def test_the_attribution_names_the_licence_the_files_and_their_hashes(build: ModuleType) -> None:
    downloads = {
        s.key: build.Download(s.url, 100 + i, f"{i:x}" * 64, "Sun, 18 Oct 2026 02:35:29 GMT")
        for i, s in enumerate(build.SOURCES)
    }
    counts = {
        "de_places": 7662,
        "postal_codes": 10794,
        "non_de_cities": 8741,
        "non_de_city_count": 871,
        "non_de_countries": 423,
    }
    text = build.render_attribution(downloads, "2026-10-18", counts)
    assert (
        "Contains data from GeoNames (geonames.org), CC BY 4.0" in text and "Downloaded on 2026-10-18" in text
    )
    assert all(
        s.url.removeprefix(build.GEONAMES + "/") in text and downloads[s.key].sha256 in text
        for s in build.SOURCES
    )
    assert "7,662 places" in text and "10,794 distinct postal codes" in text and "871 cities" in text


def test_the_committed_attribution_records_the_download_and_four_hashes() -> None:
    text = (DATA / "ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "Contains data from GeoNames (geonames.org), CC BY 4.0" in text
    assert re.search(r"Downloaded on \d{4}-\d{2}-\d{2} from <https://download.geonames.org/export/>", text)
    assert len(set(re.findall(r"`([0-9a-f]{64})`", text))) == 4
    for name in ("dump/DE.zip", "zip/DE.zip", "dump/cities15000.zip", "dump/countryInfo.txt"):
        assert f"`{name}`" in text
