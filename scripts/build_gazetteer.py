"""Build the packaged GeoNames gazetteer (spec §8.12, plan Task 4).

    uv run python scripts/build_gazetteer.py

Downloads four GeoNames files into a temporary directory (outbound HTTPS honours `HTTPS_PROXY` and
`SSL_CERT_FILE`), writes `de_places.tsv`, `non_de_places.tsv` and `ATTRIBUTION.md` into
`backend/src/jobhunter/geo/data/`, prints the provenance (download date, size and SHA-256 of every file) and
the row counts, and deletes the downloads.
The raw dumps are never kept or committed. `country_names_de.tsv` in the same directory is the hand-maintained
German input of the build. CI does not run this script; for one download its output is deterministic.

The rules (the tests in tests/unit/test_build_gazetteer.py pin them with small synthetic inputs):

- German places: GeoNames feature class `P` in `dump/DE.zip` with a population of at least 1,000, each with
  its municipality (the GeoNames admin4 code, the German municipality key: a city's districts carry the
  city's). A place's `alt_folded` holds its folded GeoNames `alternatenames` that a German text can contain
  (Latin script, capitalised, no airport or station code) and the umlaut transliteration of its name
  (`ä`->`ae`, `ö`->`oe`, `ü`->`ue`, `ß`->`ss`), so `moenchengladbach` finds Mönchengladbach.
- Postal codes (`zip/DE.zip`): a code maps to the places with the same folded name (any spelling) in the same
  admin1 code as one of its rows, else to the nearest German place within 10 km, else to nothing. The brief's
  rule is refined three ways, see `attach_postal_codes` and `_named`: it is applied per code, not per row;
  large-customer rows (no accuracy) count only for a code without a place row; and among homonyms the exact
  spelling wins, then the places within 10 km. The postal file writes the federal state as an ISO letter code
  in some rows and as the GeoNames number in others; both become the GeoNames number.
- Outside Germany: the cities of `dump/cities15000.zip` with more than 100,000 inhabitants in a country of
  continent `EU` in `countryInfo.txt` (Germany excluded), each under its name and its usable alternate names,
  plus every country name in English from `countryInfo.txt` and in German from `country_names_de.tsv`.
"""

import hashlib
import sys
import tempfile
import zipfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx
from jobhunter.geo.gazetteer import NON_DE_COLUMNS, PLACE_COLUMNS, NonDePlace, Place, distance_km
from jobhunter.normalize.text import fold

GEONAMES: Final = "https://download.geonames.org/export"
DATA_DIR: Final = Path(__file__).resolve().parents[1] / "backend/src/jobhunter/geo/data"

MIN_DE_POPULATION: Final = 1_000
MIN_CITY_POPULATION: Final = 100_000  # "over": strictly more
POSTAL_NEAREST_KM: Final = 10.0
_GRID_DEGREES: Final = 0.2  # a cell is wider than 10 km in both directions at German latitudes

# GeoNames' main name of these cities is an English exonym. The German name is the display name; the GeoNames
# name stays findable as an alternate spelling. The build fails if one of the ids is not in the dump.
NAME_OVERRIDES: Final = {"2867714": "München", "2861650": "Nürnberg"}

# The federal state in the postal file, by ISO 3166-2:DE suffix, as GeoNames' admin1 number.
ISO_STATE_TO_ADMIN1: Final = {
    "BW": "01",
    "BY": "02",
    "HB": "03",
    "HH": "04",
    "HE": "05",
    "NI": "06",
    "NW": "07",
    "RP": "08",
    "SL": "09",
    "SH": "10",
    "BB": "11",
    "MV": "12",
    "SN": "13",
    "ST": "14",
    "TH": "15",
    "BE": "16",
}
_ADMIN1_NUMBERS: Final = frozenset(ISO_STATE_TO_ADMIN1.values())

_UMLAUTS: Final = {
    ord("ä"): "ae",
    ord("ö"): "oe",
    ord("ü"): "ue",
    ord("ß"): "ss",
    ord("Ä"): "Ae",
    ord("Ö"): "Oe",
    ord("Ü"): "Ue",
    ord("ẞ"): "SS",
}
_ALIAS_CHARACTERS: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789 .'()/-")


@dataclass(frozen=True)
class Source:
    key: str
    url: str
    member: str | None  # the file inside a zip, None for a plain text file


SOURCES: Final = (
    Source("de_dump", f"{GEONAMES}/dump/DE.zip", "DE.txt"),
    Source("de_postal", f"{GEONAMES}/zip/DE.zip", "DE.txt"),
    Source("cities15000", f"{GEONAMES}/dump/cities15000.zip", "cities15000.txt"),
    Source("country_info", f"{GEONAMES}/dump/countryInfo.txt", None),
)


@dataclass(frozen=True)
class Download:
    url: str
    size: int
    sha256: str
    last_modified: str


@dataclass(frozen=True)
class PostalRow:
    code: str
    place: str
    admin1: str | None  # the GeoNames admin1 number, None when the row has no state
    lat: float
    lon: float
    institution: bool  # the row names an institution (a large-customer code), not a place: it has no accuracy


# --- parsing --------------------------------------------------------------------------------------


def geonames_rows(text: str, cells: int) -> list[list[str]]:
    """The tab-separated rows of a GeoNames text file, without `#` comment lines and blank lines."""
    rows = [line.split("\t") for line in text.split("\n") if line and not line.startswith("#")]
    for number, row in enumerate(rows, start=1):
        if len(row) != cells:
            raise ValueError(f"row {number}: {len(row)} cells, expected {cells}")
    return rows


def umlaut_transliteration(name: str) -> str:
    """`Mönchengladbach` -> `Moenchengladbach`; unlike `fold`, which gives `monchengladbach`."""
    return name.translate(_UMLAUTS)


def usable_alias(raw: str) -> str | None:
    """The folded form of an alternate name that a German text can contain, else None.

    GeoNames lists a place's names in every language and script, with airport and station codes (`BER`) and
    romanisations of other scripts (`berurin`) mixed in. Only Latin-script names that start with a capital and
    are not a short all-capitals code are kept; the rest can never match a German location string.
    """
    raw = raw.strip().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    if not raw or not raw[0].isupper() or (raw.isupper() and len(raw) <= 4):
        return None
    folded = fold(raw)
    if not folded or folded.isdigit() or not set(folded) <= _ALIAS_CHARACTERS or folded[0] in " .'()/-":
        return None
    return folded


def aliases(alternatenames: str, *extra: str) -> list[str]:
    """The sorted folded alternate names (GeoNames' comma-separated cell) and `extra` names."""
    found = {usable_alias(part) for part in alternatenames.split(",")}
    found.update(fold(name) for name in extra)
    found.discard(None)
    found.discard("")
    return sorted(a for a in found if a is not None)


def de_places(dump_text: str) -> list[Place]:
    """The German places of the main dump, with their names and without postal codes yet."""
    places: list[Place] = []
    seen_overrides: set[str] = set()
    for row in geonames_rows(dump_text, 19):
        geoname_id, name, _ascii, alternatenames, lat, lon, feature_class, _code, country = row[:9]
        population = int(row[14] or 0)
        if feature_class != "P" or country != "DE" or population < MIN_DE_POPULATION:
            continue
        display = NAME_OVERRIDES.get(geoname_id, name)
        if geoname_id in NAME_OVERRIDES:
            seen_overrides.add(geoname_id)
        folded = fold(display)
        spellings = aliases(alternatenames, name, umlaut_transliteration(display))
        places.append(
            Place(
                gazetteer_id=f"geonames:{geoname_id}",
                name=display,
                folded=folded,
                alt_folded=tuple(a for a in spellings if a != folded),
                country_code="DE",
                admin1=row[10],
                municipality=row[13],
                lat=float(lat),
                lon=float(lon),
                population=population,
                postal_codes=(),
            )
        )
    if seen_overrides != NAME_OVERRIDES.keys():
        raise ValueError(
            f"name overrides not found in the dump: {sorted(NAME_OVERRIDES.keys() - seen_overrides)}"
        )
    return places


def postal_rows(postal_text: str) -> list[PostalRow]:
    rows: list[PostalRow] = []
    for row in geonames_rows(postal_text, 12):
        country, code, place, _state, state_code = row[:5]
        if country != "DE" or not (len(code) == 5 and code.isdigit()):
            continue
        admin1 = ISO_STATE_TO_ADMIN1.get(state_code, state_code)
        rows.append(
            PostalRow(
                code,
                place,
                admin1 if admin1 in _ADMIN1_NUMBERS else None,
                float(row[9]),
                float(row[10]),
                institution=not row[11],
            )
        )
    return rows


# --- postal codes ---------------------------------------------------------------------------------


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return int(lat // _GRID_DEGREES), int(lon // _GRID_DEGREES)


def attach_postal_codes(
    places: Sequence[Place], postal: Iterable[PostalRow]
) -> tuple[list[Place], Counter[str]]:
    """The places with their postal codes, and how many codes mapped by name, by distance or not at all.

    A code maps to the places named by its rows (same folded name, any spelling, same admin1; see `_named` for
    how homonyms are told apart); only a code that no row names maps to the nearest place within 10 km. Two
    refinements keep the file's noise out. The rule is
    per code, not per row, so a row that names nothing never adds a place to a code that another row names.
    And the file lists a code once per institution too (a large-customer row: the institution's name and the
    address of one of its offices, often far from the town and without an accuracy), so those rows count
    only for a code that has no place row, and the distance rule looks at the most central row of the code.
    """
    by_name: dict[tuple[str, str], list[Place]] = {}
    grid: dict[tuple[int, int], list[Place]] = {}
    for place in places:
        for name in (place.folded, *place.alt_folded):
            by_name.setdefault((place.admin1, name), []).append(place)
        grid.setdefault(_cell(place.lat, place.lon), []).append(place)

    rows_by_code: dict[str, list[PostalRow]] = {}
    for row in postal:
        rows_by_code.setdefault(row.code, []).append(row)

    codes: dict[str, set[str]] = {place.gazetteer_id: set() for place in places}
    stats: Counter[str] = Counter()
    for code, all_rows in sorted(rows_by_code.items()):
        rows = [r for r in all_rows if not r.institution] or all_rows
        targets = [p for row in rows if row.admin1 for p in _named(by_name, row)]
        if targets:
            stats["by_name"] += 1
        else:
            nearest = _nearest(grid, _most_central(rows))
            targets = [nearest] if nearest is not None else []
            stats["nearest" if targets else "unmapped"] += 1
        for place in targets:
            codes[place.gazetteer_id].add(code)
    return [replace(p, postal_codes=tuple(sorted(codes[p.gazetteer_id]))) for p in places], stats


def _named(by_name: dict[tuple[str, str], list[Place]], row: PostalRow) -> list[Place]:
    """The places a row names. Folding makes `Fürth` and `Furth` one name, so among several matches the places
    spelled exactly like the row win, and then those within 10 km of the row (two `Amberg` lie in Bavaria)."""
    found = by_name.get((row.admin1 or "", fold(row.place)), [])
    found = [p for p in found if p.name == row.place] or found
    return [p for p in found if distance_km(row.lat, row.lon, p.lat, p.lon) <= POSTAL_NEAREST_KM] or found


def _most_central(rows: Sequence[PostalRow]) -> PostalRow:
    """The row with the smallest summed distance to the others (the first of equals)."""
    return min(rows, key=lambda r: sum(distance_km(r.lat, r.lon, o.lat, o.lon) for o in rows))


def _nearest(grid: dict[tuple[int, int], list[Place]], row: PostalRow) -> Place | None:
    """The closest place within `POSTAL_NEAREST_KM` of the row (the more populous one on a tie)."""
    i, j = _cell(row.lat, row.lon)
    near = [
        (distance_km(row.lat, row.lon, p.lat, p.lon), -p.population, p.gazetteer_id, p)
        for di in (-1, 0, 1)
        for dj in (-1, 0, 1)
        for p in grid.get((i + di, j + dj), ())
    ]
    near = [n for n in near if n[0] <= POSTAL_NEAREST_KM]
    return min(near, key=lambda n: n[:3])[3] if near else None


# --- outside Germany ------------------------------------------------------------------------------


def german_country_names(text: str) -> dict[str, list[str]]:
    """`country_names_de.tsv`: country code -> its names; `#` lines and blank lines are skipped."""
    names: dict[str, list[str]] = {}
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not parts[1].strip():
            raise ValueError(f"country_names_de.tsv line {number}: expected '<code> TAB <name>'")
        names.setdefault(parts[0], []).append(parts[1].strip())
    return names


# A key can name a city (under its main name or an alternate name) and a country. A city's main name wins
# over a country name (Luxembourg), which wins over a city's alternate name.
_CITY_NAME, _COUNTRY_NAME, _CITY_ALIAS = 0, 1, 2


def non_de_places(cities_text: str, country_info_text: str, german_names_text: str) -> list[NonDePlace]:
    countries = {
        row[0]: (row[4].strip(), row[8]) for row in geonames_rows(country_info_text, 19) if row[0] != "DE"
    }
    german = german_country_names(german_names_text)
    if unknown := sorted(german.keys() - countries.keys()):
        raise ValueError(f"country_names_de.tsv names unknown or German country codes: {unknown}")
    if missing := sorted(countries.keys() - german.keys()):
        raise ValueError(f"country_names_de.tsv has no German name for: {missing}")

    # key -> (rank, -population, id, row): the smallest tuple wins
    best: dict[str, tuple[int, int, str, NonDePlace]] = {}

    def offer(key: str, rank: int, population: int, row: NonDePlace) -> None:
        if not key:
            return
        candidate = (rank, -population, row.gazetteer_id or row.country_code, row)
        if key not in best or candidate[:3] < best[key][:3]:
            best[key] = candidate

    european = {code for code, (_name, continent) in countries.items() if continent == "EU"}
    for row in geonames_rows(cities_text, 19):
        geoname_id, name, ascii_name, alternatenames, lat, lon, feature_class, _code, country = row[:9]
        population = int(row[14] or 0)
        if feature_class != "P" or country not in european or population <= MIN_CITY_POPULATION:
            continue
        city = NonDePlace(
            name=name,
            folded=fold(name),
            country_code=country,
            kind="city",
            gazetteer_id=f"geonames:{geoname_id}",
            lat=float(lat),
            lon=float(lon),
        )
        for main in (name, ascii_name):
            offer(fold(main), _CITY_NAME, population, replace(city, folded=fold(main)))
        for alias in aliases(alternatenames):
            offer(alias, _CITY_ALIAS, population, replace(city, folded=alias))

    for code, (english, _continent) in sorted(countries.items()):
        for name in (english, *german[code]):
            country = NonDePlace(name=name, folded=fold(name), country_code=code, kind="country")
            holder = best.get(country.folded)
            if holder and holder[0] == _COUNTRY_NAME and holder[3].country_code != code:
                raise ValueError(f"{name!r} names two countries: {holder[3].country_code} and {code}")
            offer(country.folded, _COUNTRY_NAME, 0, country)
    return sorted(
        (row for *_, row in best.values()), key=lambda r: (r.kind != "city", r.country_code, r.folded)
    )


# --- output ---------------------------------------------------------------------------------------


def _tsv(columns: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = ["\t".join(columns)]
    for row in rows:
        if any("\t" in cell or "\n" in cell for cell in row):
            raise ValueError(f"a TSV cell holds a tab or newline: {row!r}")
        lines.append("\t".join(row))
    return "\n".join(lines) + "\n"


def render_de_places(places: Iterable[Place]) -> str:
    return _tsv(
        PLACE_COLUMNS,
        (
            (
                p.gazetteer_id,
                p.name,
                p.folded,
                "|".join(p.alt_folded),
                p.country_code,
                p.admin1,
                p.municipality,
                repr(p.lat),
                repr(p.lon),
                str(p.population),
                "|".join(p.postal_codes),
            )
            for p in sorted(places, key=lambda p: int(p.gazetteer_id.removeprefix("geonames:")))
        ),
    )


def render_non_de_places(rows: Iterable[NonDePlace]) -> str:
    return _tsv(
        NON_DE_COLUMNS,
        (
            (
                r.name,
                r.folded,
                r.country_code,
                r.kind,
                r.gazetteer_id,
                "" if r.lat is None else repr(r.lat),
                "" if r.lon is None else repr(r.lon),
            )
            for r in rows
        ),
    )


def render_attribution(downloads: dict[str, Download], downloaded_on: str, counts: dict[str, int]) -> str:
    sources = "\n".join(
        f"| `{s.url.removeprefix(GEONAMES + '/')}` | {d.last_modified} | {d.size:,} | `{d.sha256}` |"
        for s in SOURCES
        for d in [downloads[s.key]]
    )
    return f"""\
# Attribution

Contains data from GeoNames (geonames.org), CC BY 4.0
(<https://creativecommons.org/licenses/by/4.0/>).

`de_places.tsv` and `non_de_places.tsv` are generated from the GeoNames files below by
`scripts/build_gazetteer.py`. The data was changed: it was filtered, names were folded (accents removed,
case-folded) and extended with umlaut transliterations, postal codes were attached to places, and the main
name of Munich and Nuremberg was replaced by the German one. `country_names_de.tsv` is hand-maintained and not
GeoNames data.

## Provenance

Downloaded on {downloaded_on} from <https://download.geonames.org/export/>.

| File | Last-Modified | Bytes | SHA-256 |
| --- | --- | ---: | --- |
{sources}

## Filters

- `de_places.tsv`: feature class `P` in country `DE` with a population of at least {MIN_DE_POPULATION:,},
  with its municipality (admin4) code. Alternate names: Latin script, capitalised, no short all-capitals
  code. A postal code maps to the places with the same folded name in the same state, else to the nearest
  place within {POSTAL_NEAREST_KM:g} km.
- `non_de_places.tsv`: cities of `cities15000` with more than {MIN_CITY_POPULATION:,} inhabitants in a
  country of continent `EU` other than `DE`, under their name and usable alternate names; every country name
  other than Germany in English (`countryInfo.txt`) and German (`country_names_de.tsv`).

## Rows

- `de_places.tsv`: {counts["de_places"]:,} places, {counts["postal_codes"]:,} distinct postal codes
- `non_de_places.tsv`: {counts["non_de_cities"]:,} city rows ({counts["non_de_city_count"]:,} cities),
  {counts["non_de_countries"]:,} country rows
"""


# --- download and main ----------------------------------------------------------------------------


def download(client: httpx.Client, source: Source, directory: Path) -> tuple[Download, Path]:
    target = directory / source.key
    digest = hashlib.sha256()
    size = 0
    with client.stream("GET", source.url) as response, target.open("wb") as out:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
        return Download(
            source.url, size, digest.hexdigest(), response.headers.get("last-modified", "")
        ), target


def read_text(source: Source, path: Path) -> str:
    if source.member is None:
        return path.read_text(encoding="utf-8")
    with zipfile.ZipFile(path) as archive:
        return archive.read(source.member).decode("utf-8")


def main() -> int:
    downloaded_on = datetime.now(UTC).date().isoformat()
    downloads: dict[str, Download] = {}
    texts: dict[str, str] = {}
    timeout = httpx.Timeout(30.0, read=180.0)
    with tempfile.TemporaryDirectory(prefix="gazetteer-") as tmp, httpx.Client(timeout=timeout) as client:
        for source in SOURCES:
            directory = Path(tmp) / source.key
            directory.mkdir()
            got, path = download(client, source, directory)
            downloads[source.key] = got
            texts[source.key] = read_text(source, path)
            print(f"downloaded {source.url}: {got.size:,} bytes, sha256 {got.sha256}")

    places, stats = attach_postal_codes(de_places(texts["de_dump"]), postal_rows(texts["de_postal"]))
    german_names = (DATA_DIR / "country_names_de.tsv").read_text(encoding="utf-8")
    non_de = non_de_places(texts["cities15000"], texts["country_info"], german_names)

    counts = {
        "de_places": len(places),
        "postal_codes": len({code for p in places for code in p.postal_codes}),
        "non_de_cities": sum(1 for r in non_de if r.kind == "city"),
        "non_de_city_count": len({r.gazetteer_id for r in non_de if r.kind == "city"}),
        "non_de_countries": sum(1 for r in non_de if r.kind == "country"),
    }
    (DATA_DIR / "de_places.tsv").write_text(render_de_places(places), encoding="utf-8", newline="\n")
    (DATA_DIR / "non_de_places.tsv").write_text(render_non_de_places(non_de), encoding="utf-8", newline="\n")
    (DATA_DIR / "ATTRIBUTION.md").write_text(
        render_attribution(downloads, downloaded_on, counts), encoding="utf-8", newline="\n"
    )

    print(f"downloaded on {downloaded_on}")
    print(f"de_places.tsv: {counts['de_places']} places, {counts['postal_codes']} distinct postal codes")
    print(f"postal rows: {dict(stats)}")
    print(
        f"non_de_places.tsv: {counts['non_de_cities']} city rows for {counts['non_de_city_count']} cities, "
        f"{counts['non_de_countries']} country rows"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
