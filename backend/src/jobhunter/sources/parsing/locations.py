"""Location parsing and resolution (spec §8.12, §5.3.1, §5.3.4).

`parse_location_text` turns a provider's location string into `Location`s, each with its candidates:

1. a trailing publisher suffix (` • über Stepstone`, ` via Indeed`) is dropped;
2. the text is split on `;`, `/`, ` und `, ` oder `, ` or ` and ` and `, outside parentheses; a piece that is
   a place name as it stands (`Frankfurt/Oder`) is not split further;
3. each part is resolved: a postal code (`\\b\\d{5}\\b`), explicit coordinates, then the folded name. One
   match is `unique`; several are `ambiguous` and keep every candidate (the most populous is never picked);
   none is `unresolved` and kept as a label. A trailing parenthetical (`Berlin (Stepstone)`,
   `Remote (Germany)`) is dropped unless the part is a place name with it (`Frankfurt (Oder)`); a trailing
   `, <country>` (`Berlin, Germany`, `Zürich, Schweiz`) selects the candidates in that country, but only after
   a single place name: a list, a country, a state or a marker in front of it (`Deutschland, Österreich`,
   `Remote, Austria`) stays unresolved. Other comma parts stay unresolved too: a comma separates a list as
   often as it qualifies a name, and guessing would lose locations. A federal state (`Sachsen`, `NRW`; not the
   city-states) is a region, never the town of that name: it is unresolved and never foreign. Text shaped like
   coordinates that lie outside latitude ±90 / longitude ±180 (`95.10115, 11.5`) is one unresolved label;
4. `Remote`, `Homeoffice`, `Deutschlandweit` and `Beliebiger Ort` are markers, not places: they set `kind`. A
   marker qualified by Germany's name only (`Remote, Germany`, `Germany (Remote)`) is that marker.

`ParsedLocations.positively_non_de` is the §5.3.4 evidence for `OutOfScope('not_germany')`: every part names
a place or a country outside Germany and no part has a German candidate. `positively_outside_germany` is the
same test over stored `Location`s (§11.1): every location is resolved with every candidate outside Germany.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel

from jobhunter.geo.gazetteer import Gazetteer, NonDePlace, Place, distance_km
from jobhunter.normalize.text import fold, norm_text

LocationKind = Literal["city", "multi", "country_wide", "remote", "unknown"]
Resolution = Literal["unique", "ambiguous", "unresolved"]

GERMANY: Final = "DE"
SAME_POSTAL_AREA_KM: Final = 10.0  # places that share a postal code and lie this close are one location
COORDINATE_MATCH_KM: Final = 50.0  # a point farther than this from every known place has no known country

_POSTAL_CODE: Final = re.compile(r"\b\d{5}\b")
_COORDINATES: Final = re.compile(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")
_PUBLISHER_SUFFIX: Final = re.compile(r"(?:\s*•)?\s+(?:über|via)\s+\S.*$", re.IGNORECASE)
_PUBLISHER_PIECE: Final = re.compile(r"(?:über|via)\s", re.IGNORECASE)
_TRAILING_PARENTHETICAL: Final = re.compile(r"\s*\([^()]*\)\s*$")
_SEMICOLON: Final = re.compile(r"\s*;\s*")
_CONJUNCTION: Final = re.compile(r"\s+(?:und|oder|or|and)\s+", re.IGNORECASE)
_SLASH: Final = re.compile(r"(?<=\w\w)\s*/\s*(?=\w\w)")  # not `n/a`
_HAS_CONTENT: Final = re.compile(r"\w")
# A site word after a place name (Ashby: "Berlin Office"); it is dropped when what is left is a place.
_SITE_SUFFIX: Final = re.compile(
    r"\s+(?:office|offices|büro|buero|hq|headquarters|campus|hub|site)$", re.IGNORECASE
)
DISTRICT_KM: Final = 30.0  # `<district>, <city>`: the district lies this close to the city it names

_REMOTE: Final = frozenset(
    {"remote", "homeoffice", "home office", "home-office", "remote job", "remote jobs"}
)
_COUNTRY_WIDE: Final = frozenset({"deutschlandweit", "bundesweit", "beliebiger ort", "anywhere"})
# Germany in German and English, and in the languages postings of German boards also use (V17: Arbeitnow
# writes "Hamburg, Hamburg, Duitsland").
_GERMANY_NAMES: Final = frozenset(
    {"deutschland", "germany", "bundesrepublik deutschland"}
    | {"duitsland", "allemagne", "alemania", "germania", "niemcy", "nemecko", "tyskland", "almanya"}
)
# The federal states, folded, in German and English, with their GeoNames admin1 codes. A state is a region,
# not a place: `Sachsen` must not become the village Sachsen bei Ansbach, nor `Brandenburg` the town
# Brandenburg an der Havel. The city-states Berlin, Hamburg and Bremen are left out; they are places.
_LAND_ADMIN1: Final = MappingProxyType(
    {
        **dict.fromkeys(("baden-wurttemberg", "baden-wuerttemberg", "baden wurttemberg"), "01"),
        **dict.fromkeys(("bayern", "freistaat bayern", "bavaria"), "02"),
        **dict.fromkeys(("hessen", "hesse"), "05"),
        **dict.fromkeys(("niedersachsen", "lower saxony"), "06"),
        **dict.fromkeys(
            ("nordrhein-westfalen", "nordrhein westfalen", "north rhine-westphalia", "nrw"), "07"
        ),
        **dict.fromkeys(("rheinland-pfalz", "rheinland pfalz", "rhineland-palatinate"), "08"),
        "saarland": "09",
        "schleswig-holstein": "10",
        "brandenburg": "11",
        **dict.fromkeys(
            ("mecklenburg-vorpommern", "mecklenburg vorpommern", "mecklenburg-western pomerania"), "12"
        ),
        **dict.fromkeys(("sachsen", "freistaat sachsen", "saxony"), "13"),
        **dict.fromkeys(("sachsen-anhalt", "saxony-anhalt"), "14"),
        **dict.fromkeys(("thuringen", "thueringen", "freistaat thuringen", "thuringia"), "15"),
    }
)
_LAND_NAMES: Final = frozenset(_LAND_ADMIN1)


class Candidate(BaseModel):
    gazetteer_id: str
    country_code: str
    lat: float
    lon: float


class Location(BaseModel):
    label: str
    resolution: Resolution
    candidates: list[Candidate]
    postal_code: str | None
    source: str


@dataclass(frozen=True)
class ParsedLocations:
    locations: list[Location]
    kind: LocationKind
    positively_non_de: bool


@dataclass(frozen=True)
class _Part:
    """One resolved part: a location, or a marker (`location` is None)."""

    location: Location | None
    marker: Literal["remote", "country_wide"] | None
    non_de: bool
    country_only: bool = False  # the part is a country's name and nothing else (`Germany`, `Norway`)


def _candidate(place: Place | NonDePlace) -> Candidate | None:
    if isinstance(place, Place):
        return Candidate(
            gazetteer_id=place.gazetteer_id, country_code=place.country_code, lat=place.lat, lon=place.lon
        )
    if place.kind == "city" and place.lat is not None and place.lon is not None:
        return Candidate(
            gazetteer_id=place.gazetteer_id, country_code=place.country_code, lat=place.lat, lon=place.lon
        )
    return None


def _resolution(candidates: Sequence[Candidate]) -> Resolution:
    return "unresolved" if not candidates else "unique" if len(candidates) == 1 else "ambiguous"


def _by_name(gaz: Gazetteer, folded: str) -> list[Candidate]:
    """Every candidate for a folded name: the German places by population, then the non-German city."""
    places: list[Place | NonDePlace] = [*gaz.by_name(folded)]
    outside = gaz.non_de(folded)
    if outside is not None:
        places.append(outside)
    return [c for p in places if (c := _candidate(p)) is not None]


def _country_of(gaz: Gazetteer, folded: str) -> str | None:
    """The country that `folded` names (a country name only), as an ISO code."""
    if folded in _GERMANY_NAMES:
        return GERMANY
    outside = gaz.non_de(folded)
    return outside.country_code if outside is not None and outside.kind == "country" else None


def _is_known(gaz: Gazetteer, folded: str) -> bool:
    return (
        folded in _REMOTE
        or folded in _COUNTRY_WIDE
        or gaz.is_german_place_name(folded)
        or gaz.non_de(folded) is not None
    )


def _in_country(candidates: list[Candidate], country: str | None) -> list[Candidate]:
    """The candidates in `country`; all of them when `country` is unknown or none lies in it."""
    if country is None:
        return candidates
    return [c for c in candidates if c.country_code == country] or candidates


def _mask_parentheses(text: str) -> str:
    """`text` with everything inside parentheses blanked, so separators in there are not found."""
    depth = 0
    masked = []
    for ch in text:
        if ch == "(":
            depth += 1
        masked.append("_" if depth else ch)
        if ch == ")" and depth:
            depth -= 1
    return "".join(masked)


def _cut(text: str, separator: re.Pattern[str]) -> list[str]:
    pieces: list[str] = []
    start = 0
    for m in separator.finditer(_mask_parentheses(text)):
        pieces.append(text[start : m.start()])
        start = m.end()
    pieces.append(text[start:])
    return pieces


def _split_parts(text: str, gaz: Gazetteer) -> list[str]:
    """The parts of a location string, split at `;`, then at the conjunctions, then at `/`. A piece that is a
    place name as it stands (`Frankfurt/Oder`) is kept whole at every level."""

    def split(piece: str, separators: Sequence[re.Pattern[str]]) -> list[str]:
        if not separators or _is_known(gaz, fold(piece.strip())):
            return [piece]
        return [part for cut in _cut(piece, separators[0]) for part in split(cut, separators[1:])]

    parts = split(text, (_SEMICOLON, _CONJUNCTION, _SLASH))
    return [p.strip(" ,") for p in parts if _HAS_CONTENT.search(p)]


def _resolve_postal_code(gaz: Gazetteer, code: str) -> list[Candidate] | None:
    """The candidates of a postal code: one place, or places within 10 km of each other (their centroid)."""
    places = gaz.by_postal_code(code)
    if not places:
        return None
    candidates = [c for p in places if (c := _candidate(p)) is not None]
    close = all(
        distance_km(a.lat, a.lon, b.lat, b.lon) <= SAME_POSTAL_AREA_KM
        for i, a in enumerate(places)
        for b in places[i + 1 :]
    )
    if len(places) > 1 and close:
        main = places[0]  # the most populous
        return [
            Candidate(
                gazetteer_id=main.gazetteer_id,
                country_code=main.country_code,
                lat=sum(p.lat for p in places) / len(places),
                lon=sum(p.lon for p in places) / len(places),
            )
        ]
    return candidates


def _resolve_name(gaz: Gazetteer, text: str, country_hint: str | None) -> tuple[list[Candidate], bool]:
    """The candidates of a place name and whether the text positively names somewhere outside Germany."""
    folded = fold(text)
    if folded in _LAND_NAMES:
        return [], False
    candidates = _by_name(gaz, folded)
    country = _country_of(gaz, folded)
    if not candidates and country is None and "," in text:
        return _resolve_qualified(gaz, text, country_hint)
    return _in_country_result(candidates, country, country_hint)


def _in_country_result(
    candidates: list[Candidate], country: str | None, country_hint: str | None
) -> tuple[list[Candidate], bool]:
    """The candidates in `country`. When none lies there, only the German ones are kept: a qualifier that
    contradicts the name never turns a German place foreign (`Berlin, Austria`), nor moves a city to a third
    country (`Santa Cruz, Bolivia` is no Spanish Santa Cruz)."""
    if country is not None and not any(c.country_code == country for c in candidates):
        candidates = [c for c in candidates if c.country_code == GERMANY]
    candidates = _in_country(_in_country(candidates, country), country_hint)
    if candidates:
        return candidates, all(c.country_code != GERMANY for c in candidates)
    return [], country is not None and country != GERMANY


def _german(gaz: Gazetteer, folded: str, *, admin1: str | None) -> list[Candidate]:
    """The German places named `folded`, those in the federal state `admin1` when there are any."""
    places = gaz.by_name(folded)
    inside = [p for p in places if p.admin1 == admin1] if admin1 is not None else []
    return [c for p in inside or places if (c := _candidate(p)) is not None]


def _german_near(gaz: Gazetteer, district: str, city: str) -> list[Candidate]:
    """`<district>, <city>`: the German places named `district` that are districts of a `city` place: in its
    municipality (the same GeoNames municipality key), smaller than it and within `DISTRICT_KM` of it.

    Two cities near each other are a list, not a district and its city, in either order (`Köln, Bonn` and
    `Bonn, Köln`, `Potsdam, Berlin`, `Offenbach, Frankfurt`; ruling T6-c): each is its own municipality, so
    the part stays unresolved (§8.12: positive evidence only)."""
    cities = gaz.by_name(city)
    near = [
        d
        for d in gaz.by_name(district)
        if any(
            d.gazetteer_id != c.gazetteer_id
            and d.municipality != ""  # an empty key never matches (F1 review, Minor 4)
            and d.municipality == c.municipality
            and d.population < c.population
            and distance_km(d.lat, d.lon, c.lat, c.lon) <= DISTRICT_KM
            for c in cities
        )
    ]
    return [c for p in near if (c := _candidate(p)) is not None]


def _resolve_qualified(gaz: Gazetteer, text: str, country_hint: str | None) -> tuple[list[Candidate], bool]:
    """A place qualified by commas, resolved only where the gazetteer confirms the qualifier (V17):

    - `<place>, <country>` (`Zürich, Schweiz`): the candidates in that country;
    - `<city>, <federal state>` (`Munich, Bavaria`): the German candidates in that state;
    - `<district>, <city>` (`Kreuzberg, Berlin`): the German candidates in that city's municipality, near it
      and smaller than it;
    - `<city>, <state>, <country>` (`Detmold, Nordrhein-Westfalen, Deutschland`; `Berlin, Berlin, Germany`, a
      city-state under its own name; `Amsterdam, Noord-Holland, Netherlands`): the city in that state or
      country. Abroad the middle must be no German place, state, country or marker.

    Empty parts (`Barcelona, , Spain`) are dropped. Anything else is a list or a guess and stays unresolved:
    `Berlin, Hamburg`, `Berlin, Hamburg, Austria`, and a head that is a country, a state or a marker
    (`Deutschland, Österreich`, `Remote, Austria`), which says nothing about one place.
    """
    parts = [part for part in (norm_text(piece).strip() for piece in text.split(",")) if part]
    folded = [fold(part) for part in parts]
    if not 2 <= len(parts) <= 3 or _is_not_a_place(gaz, folded[0]):
        return [], False
    head, tail = folded[0], folded[-1]
    country = _country_of(gaz, tail)
    if len(parts) == 2:
        if country is not None:
            return _in_country_result(_by_name(gaz, head), country, country_hint)
        if (admin1 := _LAND_ADMIN1.get(tail)) is not None:
            return _german(gaz, head, admin1=admin1), False
        return _german_near(gaz, head, tail), False
    middle = folded[1]
    if country == GERMANY:
        if (admin1 := _LAND_ADMIN1.get(middle)) is not None:
            return _german(gaz, head, admin1=admin1), False
        return (_german(gaz, head, admin1=None), False) if middle == head else ([], False)
    if country is None or gaz.is_german_place_name(middle) or _is_not_a_place(gaz, middle):
        return [], False
    return _in_country_result(_by_name(gaz, head), country, country_hint)


def _is_not_a_place(gaz: Gazetteer, folded: str) -> bool:
    """A country, a federal state or a marker."""
    return (
        _country_of(gaz, folded) is not None
        or folded in _LAND_NAMES
        or folded in _REMOTE
        or folded in _COUNTRY_WIDE
    )


_QUALIFIER_PIECES: Final = re.compile(r"[,()]|\s[-\u2013\u2014]\s")  # a comma, brackets or a spaced dash


def _marker_in_germany(part: str) -> Literal["remote", "country_wide"] | None:
    """The marker of a part that is a marker qualified by Germany's name only (fix wave F1, ledger L48):
    `Remote, Germany`, `Germany (Remote)`, `Deutschland, remote`, `Remote - Deutschland`. Germany names no
    place, so the part is the marker, as `Remote (Germany)` already was."""
    pieces = [fold(piece) for piece in _QUALIFIER_PIECES.split(part) if _HAS_CONTENT.search(piece)]
    if len(pieces) < 2 or not all(p in _REMOTE or p in _COUNTRY_WIDE or p in _GERMANY_NAMES for p in pieces):
        return None
    if any(p in _COUNTRY_WIDE for p in pieces):
        return "country_wide"
    return "remote" if any(p in _REMOTE for p in pieces) else None


def _resolve_part(gaz: Gazetteer, part: str, source: str, country_hint: str | None) -> _Part:
    if (marker := _marker_in_germany(part)) is not None:
        return _Part(None, marker, False)
    if not _is_known(gaz, fold(part)):
        part = _TRAILING_PARENTHETICAL.sub("", part).strip(" ,") or part
    if not _is_known(gaz, fold(part)) and _is_known(gaz, fold(site := _SITE_SUFFIX.sub("", part))):
        part = site
    folded = fold(part)
    if folded in _REMOTE:
        return _Part(None, "remote", False)
    if folded in _COUNTRY_WIDE:
        return _Part(None, "country_wide", False)

    # coordinates before the postal code: `47.80331` holds a 5-digit run
    if (pair := _COORDINATES.fullmatch(part)) is not None:
        lat, lon = float(pair[1]), float(pair[2])
        if not _is_coordinate_pair(lat, lon):
            # Text shaped like coordinates that no point on Earth has (`95.10115, 11.5`) is one unresolved
            # label: not an error (one bad string must not fail a job), nor a postal code from its digits.
            nowhere = Location(
                label=part, resolution="unresolved", candidates=[], postal_code=None, source=source
            )
            return _Part(nowhere, None, False)
        point = location_from_coordinates(part, lat, lon, source=source)
        return _Part(point, None, bool(point.candidates) and point.candidates[0].country_code != GERMANY)

    postal_code: str | None = None
    candidates: list[Candidate] | None = None
    name = part
    if (found := _POSTAL_CODE.search(part)) is not None:
        postal_code = found.group()
        candidates = _resolve_postal_code(gaz, postal_code)
        name = norm_text(_POSTAL_CODE.sub(" ", part)).strip(" ,-")
    non_de = False
    if candidates is None:
        candidates, non_de = _resolve_name(gaz, name, country_hint) if name else ([], False)
    location = Location(
        label=part,
        resolution=_resolution(candidates),
        candidates=candidates,
        postal_code=postal_code,
        source=source,
    )
    country_only = not candidates and postal_code is None and _country_of(gaz, fold(name)) is not None
    return _Part(location, None, non_de, country_only)


def _parse(text: str, *, source: str, country_hint: str | None) -> ParsedLocations:
    gaz = Gazetteer.load()
    cleaned = _PUBLISHER_SUFFIX.sub("", norm_text(text))
    parts: list[_Part] = []
    seen: set[str] = set()
    for raw in _split_parts(cleaned, gaz):
        part = _resolve_part(gaz, raw, source, country_hint)
        key = fold(part.location.label if part.location else raw)
        if key not in seen:
            seen.add(key)
            parts.append(part)
    if not parts:
        return ParsedLocations([], "unknown", False)
    locations = [p.location for p in parts if p.location is not None]
    markers = {p.marker for p in parts if p.marker is not None}
    kind: LocationKind
    if not locations:
        kind = "country_wide" if "country_wide" in markers else "remote"
    elif all(p.country_only for p in parts if p.location is not None):
        kind = "country_wide"  # a whole country (V17: Ashby's `Germany`); it stays an unresolved location
    else:
        kind = "multi" if len(locations) > 1 else "city"
    return ParsedLocations(locations, kind, all(p.non_de for p in parts))


def parse_location_text(text: str, *, source: str) -> ParsedLocations:
    """Parse a provider's free-text location (spec §8.12 steps 1-4, §5.3.4)."""
    return _parse(text, source=source, country_hint=None)


def parse_jsearch_location(
    job_location: str | None, job_city: str | None, job_country: str | None
) -> ParsedLocations:
    """JSearch (spec §5.3.1): `job_city` when set (`job_country` picks between homonyms), else `job_location`.

    `job_location` reads like `Berlin     •  über Stepstone`: it is split on `•`, the `über X` / `via X`
    pieces are dropped, and `Beliebiger Ort` / `Anywhere` make the job `country_wide`.
    """
    country = (job_country or "").strip().upper()
    hint = country if len(country) == 2 and country.isalpha() else None
    if city := norm_text(job_city or ""):
        return _parse(city, source="jsearch", country_hint=hint)
    pieces = [norm_text(piece) for piece in (job_location or "").split("•")]
    kept = [p for p in pieces if p and not _PUBLISHER_PIECE.match(p)]
    return _parse("; ".join(kept), source="jsearch", country_hint=hint)


def _is_coordinate_pair(lat: float, lon: float) -> bool:
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


def location_from_coordinates(label: str, lat: float, lon: float, *, source: str) -> Location:
    """A `unique` location at explicit coordinates (spec §8.12).

    The candidate keeps the given point and takes its gazetteer id and country from the closest known place
    (German place or European city) within 50 km; farther from any, the country is unknown and the location is
    `unresolved`. Where two countries are close, the nearer place wins, and German places are far denser.
    """
    if not _is_coordinate_pair(lat, lon):
        raise ValueError(f"not a coordinate pair: {lat}, {lon}")
    place = Gazetteer.load().nearest(lat, lon, COORDINATE_MATCH_KM)
    candidate = None
    if place is not None and (known := _candidate(place)) is not None:
        candidate = Candidate(
            gazetteer_id=known.gazetteer_id, country_code=known.country_code, lat=lat, lon=lon
        )
    candidates = [candidate] if candidate is not None else []
    return Location(
        label=label,
        resolution=_resolution(candidates),
        candidates=candidates,
        postal_code=None,
        source=source,
    )


def positively_outside_germany(locations: Sequence[Location]) -> bool:
    """Every location is resolved with every candidate outside Germany, and there is at least one location."""
    return bool(locations) and all(
        loc.resolution != "unresolved"
        and bool(loc.candidates)
        and all(c.country_code not in ("", GERMANY) for c in loc.candidates)
        for loc in locations
    )
