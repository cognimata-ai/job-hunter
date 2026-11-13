"""`docs/PRIVACY.md`, the privacy statement of spec §20.6 (plan M5 Task 19, Deviations 2, 3, 18 and 19, Ruling
T19-1).

The SPA's Privacy & data page renders it with a Markdown subset and anchors its sections (Task 20), and puts
the values of the deployment's operator file in place of its operator fields' markers (M5 follow-ups Task F1:
the repository keeps the markers). So:

- its `##` sections are §20.6's ten items and `## Operator`, in that order, with no level-1 heading (the page
  has its own title, Ruling T19-3 I3);
- §5's table is §20.1's, row for row and cell for cell, but the exports' retention, in the statement's words
  with the code's 30 days for the record (Deviations 18 and 48), and the
  sentences under it name what HEAD's profile purge does beyond the table (Rulings T19-2 (3), T19-3 I1);
- §4's table is §20.6's, word for word;
- §8 carries the gazetteer's GeoNames attribution, §7 the Adzuna badge's;
- the operator fields are exactly the five markers, under `## Operator`, and nothing in it is e-mail-shaped;
  one marker for each key of the operator file, and the section says where the values come from;
- it uses only headings, paragraphs, bullet lists, tables, `**bold**` and `` `code` ``;
- the typed phrases, the export's period and its record's, and the login throttle it states are the code's;
- §2 states the client addresses HEAD logs and stores, and how long it keeps them (Rulings T19-2 (1),
  T19-3 I2);
- §6 and the UI's backup note (`settings_privacy.later`, in English and German) state the offsite lag of
  deleted data: about 4 months, the bucket's deleted object versions up to 30 days after a set ages out
  (Ruling T32-2).
"""

import json
import re
from pathlib import Path
from typing import Final

from jobhunter.api.routes.privacy import DELETE_EVERYTHING_PHRASE, PURGE_ADZUNA_PHRASE
from jobhunter.auth import throttle
from jobhunter.privacy import operator, retention
from jobhunter.privacy.export import EXPORT_ROWS_KEPT, EXPORT_TTL
from jobhunter.privacy.kinds.job_tombstone import JobTombstone
from jobhunter.privacy.kinds.profile_purge import ProfilePurge

REPO: Final = Path(__file__).resolve().parents[2]
PRIVACY: Final = REPO / "docs/PRIVACY.md"
SPEC: Final = REPO / "docs/superpowers/specs/2026-10-16-job-hunter-design.md"
GEONAMES: Final = REPO / "backend/src/jobhunter/geo/data/ATTRIBUTION.md"
CATALOGUE: Final = REPO / "frontend/src/i18n/en.json"
CATALOGUES: Final = {lang: REPO / f"frontend/src/i18n/{lang}.json" for lang in ("en", "de")}
COMPOSE: Final = REPO / "deploy/compose.dev.yaml"

SECTIONS: Final = (
    "## 1. Purpose and controller",
    "## 2. Data categories",
    "## 3. Processing and minimisation",
    "## 4. Third parties and transfers",
    "## 5. Retention",
    "## 6. Deletion and export",
    "## 7. Adzuna",
    "## 8. GeoNames",
    "## 9. Security measures",
    "## 10. How to report a concern",
    "## Operator",
)
SPEC_RETENTION: Final = "### 20.1 Retention table"
SPEC_PRIVACY_MD: Final = "### 20.6 PRIVACY.md (contents)"
# The exports row's retention: §20.1's rule (Deviations 18 and 48) in the statement's words, which say what
# the exception to "every purge" means for the user; the record without its archive goes `EXPORT_ROWS_KEPT`
# later.
EXPORTS_RETENTION: Final = (
    "7 days; deleted by every user-requested purge and by delete-everything, not by the nightly job "
    "tombstoning, so an export can hold content tombstoned after it was made until it expires; its record, "
    f"without the archive, {EXPORT_ROWS_KEPT.days} days after it expires"
)
# Under §5's table: the profile purge deletes the exports (T19-2 (3)) and spares four kinds of version
# (T19-3 I1).
PROFILE_PURGE_EXPORTS: Final = (
    "The automatic purge of an old profile version's content deletes every export too, as every purge does "
    "except the nightly job tombstoning"
)
PROFILE_PURGE_SPARES: Final = (
    "The profile purge never takes the active profile version, your newest confirmed version, the version "
    "confirmed before it, or the synthetic profile versions of the scoring's evaluation"
)
PROFILE_PURGE_SPARES_SHORT: Final = (
    "unless it is the active version, your newest confirmed version, the version confirmed before it, or a "
    "synthetic one"
)
# §2: what HEAD logs and stores of the client address (T19-2 (1), T19-3 I2): since M5 Task 21 (Ruling T21-1
# (b)) no request log at all (uvicorn's access log off, Caddy with none and a filter on its error lines), and
# since the final review (M1) no address in the sign-in and re-authentication events either.
LOGGED_REQUESTS: Final = (
    "no log line holds the client address: neither the api nor its reverse proxy logs requests, and sign-in "
    "and re-authentication events record their outcome only"
)
# The offsite lag of deleted data (Ruling T32-2): about 3 months of retained backup sets, plus up to 30 days
# of the bucket's deleted object versions. §6 states it, and so does the UI's backup note in both languages.
OFFSITE_LAG: Final = (
    "keeps the deleted files of an aged-out set for up to 30 days more",
    "deleted data can remain in the offsite backups for about 4 months in all",
)
UI_OFFSITE_LAG: Final = {
    "en": ("for up to 30 days more", "leaves the offsite backups within about 4 months"),
    "de": ("noch bis zu 30 Tage auf", "innerhalb von etwa 4 Monaten aus den externen Sicherungen"),
}
LOG_ROTATION: Final = re.compile(r'max-size: (\d+)m, max-file: "(\d+)"')
OPERATOR_FIELDS: Final = (
    "[set at deployment: operator name]",
    "[set at deployment: contact address]",
    "[set at deployment: controller name]",
    "[set at deployment: VPS provider]",
    "[set at deployment: S3 backup provider]",
)

# The Operator section's line (F1): the values come from the server's operator file, not the repository.
OPERATOR_LINE: Final = (
    "The values below come from the deployment's operator file, which the operator keeps on the server."
)

MARKER: Final = re.compile(r"\[set at deployment:[^\]\n]*\]")
ANY_MARKER: Final = re.compile(r"set at deployment", re.IGNORECASE)
EMAIL: Final = re.compile(r"[\w.%+-]+@[\w-]+(\.[\w-]+)+")
TABLE_SEPARATOR: Final = re.compile(r"^\|(\s*:?-+:?\s*\|)+$")
CODE_SPAN: Final = re.compile(r"`[^`\n]+`")
BOLD: Final = re.compile(r"\*\*[^*\n]+\*\*")
GEONAMES_STATEMENT: Final = re.compile(r"Contains data from GeoNames \(geonames\.org\), CC BY 4\.0")
LICENCE: Final = re.compile(r"<(https://creativecommons\.org/licenses/by/4\.0/)>")

# Block forms outside the subset, by a line's start: indented code or a nested list, a quote, another bullet
# marker, a fence, an ordered list, a rule or setext underline, HTML, a link reference definition.
FOREIGN_BLOCKS: Final = (
    (re.compile(r"^\s+\S"), "an indented line (code block or nested list)"),
    (re.compile(r"^>"), "a block quote"),
    (re.compile(r"^[*+](\s|$)"), "a `*` or `+` bullet"),
    (re.compile(r"^(```|~~~)"), "a fenced code block"),
    (re.compile(r"^\d+[.)](\s|$)"), "an ordered list"),
    (re.compile(r"^(-{3,}|\*{3,}|_{3,}|=+)\s*$"), "a rule or a setext heading"),
    (re.compile(r"^#\s"), "a level-1 heading (the page has its own title)"),
    (re.compile(r"^#{4,}\s"), "a heading below level 3"),
    (re.compile(r"^\[[^\]]+\]:"), "a link reference definition"),
)
# Inline forms outside the subset, in a line's text outside its code spans.
FOREIGN_INLINE: Final = (
    (re.compile(r"<[A-Za-z/!?]"), "HTML or an autolink"),
    (re.compile(r"&(#\d+|#x[0-9A-Fa-f]+|[A-Za-z]+);"), "an HTML entity"),
    (re.compile(r"!\["), "an image"),
    (re.compile(r"\]\(|\]\["), "a link"),
    (re.compile(r"https?://|www\."), "a bare URL (autolinked)"),
    (re.compile(r"\\"), "a backslash escape"),
    (re.compile(r"~~"), "a strikethrough"),
    (re.compile(r"`"), "an unbalanced backtick"),
)
# Outside code spans and bold: a `*` is emphasis, and so is an `_` that is not inside a word.
EMPHASIS: Final = (re.compile(r"\*"), re.compile(r"(?<!\w)_|_(?!\w)"))


def lines_of(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def section(lines: list[str], heading: str) -> list[str]:
    """The lines under `heading`, up to the next heading."""
    start = lines.index(heading)
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        body.append(line)
    return body


def table_rows(lines: list[str]) -> list[list[str]]:
    """The first table among `lines` (indented or not): its header and rows, each a list of its cells."""
    table: list[str] = []
    for line in (raw.strip() for raw in lines):
        if line.startswith("|"):
            table.append(line)
        elif table:
            break
    assert len(table) >= 3, f"no table: {table}"
    assert TABLE_SEPARATOR.match(table[1]), table[1]
    return [[cell.strip() for cell in line.strip("|").split("|")] for line in (table[0], *table[2:])]


def test_the_ten_sections_and_the_operator_section_in_order() -> None:
    lines = lines_of(PRIVACY)
    assert [line for line in lines if line.startswith("## ")] == list(SECTIONS)
    assert [line for line in lines if line.startswith("# ")] == [], "no level-1 heading"


def test_the_retention_table_matches_spec_20_1() -> None:
    spec = table_rows(section(lines_of(SPEC), SPEC_RETENTION))
    assert spec[0] == ["Data", "Location", "Retention", "Purge mechanism"]
    assert len(spec) > 30, spec
    assert [row[0] for row in spec].count("Exports") == 1
    expected = [[row[0], row[1], EXPORTS_RETENTION, row[3]] if row[0] == "Exports" else row for row in spec]
    stated = table_rows(section(lines_of(PRIVACY), "## 5. Retention"))
    assert [row[0] for row in stated] == [row[0] for row in expected], "§20.1's rows, in its order"
    assert stated == expected


def test_the_third_party_table_matches_spec_20_6() -> None:
    spec = table_rows(section(lines_of(SPEC), SPEC_PRIVACY_MD))
    assert spec[0] == ["Third party", "What it receives"]
    assert len(spec) == 8, spec
    assert table_rows(section(lines_of(PRIVACY), "## 4. Third parties and transfers")) == spec


def test_geonames_and_adzuna_attributions_are_present() -> None:
    lines = lines_of(PRIVACY)
    gazetteer = GEONAMES.read_text(encoding="utf-8")
    statement = GEONAMES_STATEMENT.search(gazetteer)
    licence = LICENCE.search(gazetteer)
    assert statement is not None and licence is not None, "the gazetteer's attribution"
    geonames = " ".join(section(lines, "## 8. GeoNames"))
    assert statement.group(0) in geonames
    assert f"`{licence.group(1)}`" in geonames
    assert "The data was changed" in geonames  # CC BY 4.0 asks that changes be indicated
    badge = json.loads(CATALOGUE.read_text(encoding="utf-8"))["badge.adzuna"]
    adzuna = " ".join(section(lines, "## 7. Adzuna"))
    assert badge == "Jobs by Adzuna"
    assert f"**{badge}**" in adzuna
    assert "personal-use terms" in adzuna


def test_the_operator_fields_are_marked_for_deployment() -> None:
    text = PRIVACY.read_text(encoding="utf-8")
    assert MARKER.findall(text) == list(OPERATOR_FIELDS)
    assert len(ANY_MARKER.findall(text)) == len(OPERATOR_FIELDS), "no other spelling of a marker"
    operator = "\n".join(section(lines_of(PRIVACY), "## Operator"))
    assert MARKER.findall(operator) == list(OPERATOR_FIELDS)
    assert EMAIL.search(text) is None, "no e-mail address before deployment"


def test_the_operator_file_fills_the_markers() -> None:
    """F1: one marker per key of the operator file (the SPA's `OPERATOR_MARKERS` maps them in this order), and
    the section's line says the values come from that file."""
    assert len(OPERATOR_FIELDS) == len(operator.FIELDS)
    lines = section(lines_of(PRIVACY), "## Operator")
    assert OPERATOR_LINE in lines
    assert "fills in these fields" not in " ".join(lines)


def test_only_the_rendered_markdown_subset_is_used() -> None:
    problems: list[str] = []
    for n, line in enumerate(lines_of(PRIVACY), 1):
        problems.extend(f"line {n}: {what}" for pattern, what in FOREIGN_BLOCKS if pattern.search(line))
        if line.startswith("|") and not line.rstrip().endswith("|"):
            problems.append(f"line {n}: a table row that does not end in `|`")
        text = CODE_SPAN.sub("", line)
        problems.extend(f"line {n}: {what}" for pattern, what in FOREIGN_INLINE if pattern.search(text))
        plain = BOLD.sub("", text)
        if any(pattern.search(plain) for pattern in EMPHASIS):
            problems.append(f"line {n}: emphasis other than bold")
    assert not problems, "\n".join(problems)


def test_the_phrases_the_export_period_and_the_throttle_are_the_codes() -> None:
    lines = lines_of(PRIVACY)
    deletion = " ".join(section(lines, "## 6. Deletion and export"))
    assert f"`{DELETE_EVERYTHING_PHRASE}`" in deletion
    assert f"`{PURGE_ADZUNA_PHRASE}`" in " ".join(section(lines, "## 7. Adzuna"))
    assert EXPORT_TTL.total_seconds() == EXPORT_TTL.days * 86400
    assert f"for {EXPORT_TTL.days} days" in deletion
    assert EXPORT_ROWS_KEPT.total_seconds() == EXPORT_ROWS_KEPT.days * 86400
    assert f"is deleted {EXPORT_ROWS_KEPT.days} days after it expires" in deletion
    security = " ".join(section(lines, "## 9. Security measures"))
    window = int(throttle.IP_WINDOW.total_seconds() // 60)
    lock = int(throttle.IP_LOCK.total_seconds() // 60)
    assert f"{throttle.IP_MAX_FAILURES} failed attempts within {window} minutes" in security
    assert f"lock the address out for {lock} minutes" in security
    global_window = int(throttle.GLOBAL_WINDOW.total_seconds() // 3600)
    assert f"{throttle.GLOBAL_MAX_FAILURES} within {global_window} hour" in security
    global_lock = int(throttle.GLOBAL_LOCK.total_seconds() // 3600)
    assert throttle.GLOBAL_LOCK.total_seconds() == global_lock * 3600
    assert f"lock everyone out for {global_lock} hour" in security


def test_the_profile_purges_exports_and_exceptions_are_stated() -> None:
    assert ProfilePurge().deletes_exports and not JobTombstone().deletes_exports, "HEAD's rule (Deviation 18)"
    lines = lines_of(PRIVACY)
    retained = " ".join(section(lines, "## 5. Retention"))
    assert PROFILE_PURGE_EXPORTS in retained
    assert PROFILE_PURGE_SPARES in retained
    assert PROFILE_PURGE_SPARES_SHORT in " ".join(section(lines, "## 6. Deletion and export"))


def test_the_logged_client_addresses_and_their_retention_are_stated() -> None:
    categories = " ".join(section(lines_of(PRIVACY), "## 2. Data categories"))
    assert LOGGED_REQUESTS in categories
    windows = {rule.name: rule.window for rule in retention.RULES}
    assert f"kept until {windows['sessions'].days} day after they expire" in categories
    assert f"kept {windows['login_attempts'].days} days" in categories
    rotation = LOG_ROTATION.search(COMPOSE.read_text(encoding="utf-8"))
    assert rotation is not None, "the services' log rotation"
    assert (
        f"rotated by size, at {rotation.group(2)} files of {rotation.group(1)} MB per service" in categories
    )


def test_the_offsite_lag_of_deleted_data_is_stated() -> None:
    deletion = " ".join(section(lines_of(PRIVACY), "## 6. Deletion and export"))
    assert [phrase for phrase in OFFSITE_LAG if phrase not in deletion] == []
    for lang, phrases in UI_OFFSITE_LAG.items():
        note = json.loads(CATALOGUES[lang].read_text(encoding="utf-8"))["settings_privacy.later"]
        assert [phrase for phrase in phrases if phrase not in note] == [], lang
