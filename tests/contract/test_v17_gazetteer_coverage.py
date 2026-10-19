# tests/contract/test_v17_gazetteer_coverage.py
import csv
import html
import json
import re
from collections.abc import Iterator
from pathlib import Path

from jobhunter.sources.parsing.locations import ParsedLocations, parse_jsearch_location, parse_location_text
from jobhunter.types import JSONValue

ROOT = Path("tests/contract/fixtures")
LABELS = ROOT / "location_labels.tsv"
ATS = ("greenhouse", "lever", "ashby", "personio", "recruitee", "smartrecruiters")
# The location text of each source, as the key path of the JSON body (list indices left out): the text fields
# and the structured locality fields of §5.3 that an adapter parses as a place. A JSearch string is
# `job_location`, parsed without `job_city`/`job_country` (both are null in the recordings).
LOCATION_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "jsearch": (("job_location",),),
    "serpapi": (("jobs_results", "location"),),
    "adzuna": (("results", "location", "display_name"),),
    "arbeitnow": (("data", "location"),),
    "greenhouse": (("jobs", "location", "name"), ("offices", "name")),
    "lever": (("categories", "location"), ("categories", "allLocations")),
    "ashby": (("jobs", "location"), ("secondaryLocations", "location"), ("postalAddress", "addressLocality")),
    "recruitee": (("offers", "location"), ("offers", "city")),
    "smartrecruiters": (("location", "fullLocation"), ("location", "city")),
}
# The Personio offices: `<office>` of a position and of its `<additionalOffices>` (text or CDATA).
PERSONIO_OFFICE = re.compile(r"<office>\s*(?:<!\[CDATA\[(.*?)\]\]>|([^<]*))\s*</office>", re.DOTALL)


def _strings(doc: JSONValue, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
    """Every string of `doc` with its key path, list indices left out."""
    if isinstance(doc, str):
        yield path, doc
    elif isinstance(doc, dict):
        for key, value in doc.items():
            yield from _strings(value, (*path, key))
    elif isinstance(doc, list):
        for value in doc:
            yield from _strings(value, path)


def _source_of(path: Path) -> str:
    """The parser source of a fixture body: its directory, or for `ats/` the board type of the case name."""
    top = path.relative_to(ROOT).parts[0]
    return path.name.split("_", 1)[0] if top == "ats" else top


def fixture_location_strings() -> set[tuple[str, str]]:
    """(source, string) for every distinct non-empty location string of the scrubbed fixture bodies."""
    found: set[tuple[str, str]] = set()
    for path in sorted(ROOT.rglob("*")):
        if path.name.endswith((".headers.json", ".meta.json")) or path.suffix not in (".json", ".xml"):
            continue
        source = _source_of(path)
        if path.suffix == ".xml":
            if source == "personio" and "entity" not in path.name:  # the entity variant is refused unread
                for m in PERSONIO_OFFICE.finditer(path.read_text(encoding="utf-8")):
                    office = (m.group(1) if m.group(1) is not None else html.unescape(m.group(2))).strip()
                    if office:
                        found.add((source, office))
            continue
        doc: JSONValue = json.loads(path.read_text(encoding="utf-8"))
        suffixes = LOCATION_PATHS.get(source, ())
        found |= {
            (source, value.strip())
            for key_path, value in _strings(doc)
            if value.strip() and any(key_path[-len(s) :] == s for s in suffixes)
        }
    return found


def _parse(source: str, string: str) -> ParsedLocations:
    if source == "jsearch":
        return parse_jsearch_location(string, None, None)
    return parse_location_text(string, source=source)


def _rows() -> list[dict[str, str]]:
    with LABELS.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE))


# (source, string, country, kind, parsed) per row of location_labels.tsv, parsed with the matching parser
# (JSearch strings through parse_jsearch_location)
def labelled() -> list[tuple[str, str, str, str, ParsedLocations]]:
    return [
        (r["source"], r["string"], r["country"], r["kind"], _parse(r["source"], r["string"])) for r in _rows()
    ]


def test_labels_cover_every_fixture_location_string() -> None:
    # every distinct location string found in the scrubbed fixtures has a row in location_labels.tsv (the
    # label file cannot go stale), and every row is a string of the fixtures, labelled once with a known kind
    rows = _rows()
    keys = [(r["source"], r["string"]) for r in rows]
    found = fixture_location_strings()
    assert found, "no location string found in the fixtures"
    assert sorted(found - set(keys)) == [], "fixture location strings without a label"
    assert sorted(set(keys) - found) == [], "labels of strings that no fixture holds"
    assert len(keys) == len(set(keys)), "a string is labelled twice"
    assert all(r["kind"] in ("city", "multi", "country_wide", "remote", "unknown") for r in rows)
    assert all(
        (r["country"] == "-") == (r["kind"] in ("remote", "country_wide", "unknown"))
        or (r["kind"] == "multi" and r["country"] == "-")
        for r in rows
    )
    assert all(r["country"] == "-" or (len(r["country"]) == 2 and r["country"].isupper()) for r in rows)
    assert all(r["source"] in (*LOCATION_PATHS, "personio") for r in rows) and set(ATS) <= {
        *LOCATION_PATHS,
        "personio",
    }


def test_v17_coverage_thresholds_against_hand_labels() -> None:
    rows = labelled()
    german = [
        (s, p) for _, s, country, kind, p in rows if country == "DE" and kind in ("city", "multi")
    ]  # the denominator is the label, not the parser
    resolved = [
        s
        for s, p in german
        if p.kind in ("city", "multi") and all(loc.resolution != "unresolved" for loc in p.locations)
    ]
    assert len(german) >= 40 and len(resolved) / len(german) >= 0.95
    assert not [
        s for _, s, country, _, p in rows if country == "DE" and p.positively_non_de
    ]  # no German location is ever "positively outside"
    assert not [
        s
        for _, s, country, _, p in rows
        if country not in ("DE", "-")
        and not p.positively_non_de
        and p.locations
        and all(loc.candidates for loc in p.locations)
        and all(c.country_code == "DE" for loc in p.locations for c in loc.candidates)
    ]  # a foreign city resolved as only-German is a false German; an unresolved one passes (§8.12)
    assert all(p.kind == kind for _, _, _, kind, p in rows if kind in ("remote", "country_wide"))
