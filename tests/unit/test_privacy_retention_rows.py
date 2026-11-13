"""Privacy & data's retention table (plan M4 Task 31, Ruling T31-c): the page states spec §20.1, never less.

`frontend/src/system/PrivacySection.tsx` renders `RETENTION_ROWS`, each a row of the catalogue
(`settings_privacy.row.<id>`, what is kept; `settings_privacy.kept.<id>`, for how long). The spec's §20.1
table is parsed from `docs/superpowers/specs/2026-10-16-job-hunter-design.md`, and `COVERED` maps each of its
rows to the UI row that states it (a UI row may state several spec rows, then it states each one's period).
So:

- a spec row added without a UI row fails (`COVERED` lacks it), and a UI row that states no spec row fails;
- each spec row's periods (its `<n> day(s)` and `<n> month(s)`, and "indefinitely") appear in its UI row's
  text, in English and in German, but those `EXEMPT` names with its reason;
- every UI row has its two strings in both languages.
"""

import json
import re
from pathlib import Path
from typing import Final

import pytest

REPO: Final = Path(__file__).resolve().parents[2]
SPEC: Final = REPO / "docs/superpowers/specs/2026-10-16-job-hunter-design.md"
SECTION: Final = REPO / "frontend/src/system/PrivacySection.tsx"
CATALOGUES: Final = {lang: REPO / f"frontend/src/i18n/{lang}.json" for lang in ("en", "de")}
HEADING: Final = "### 20.1 Retention table"
ROWS_DECLARATION: Final = re.compile(
    r"^export const RETENTION_ROWS = \[(.*?)\] as const;$", re.MULTILINE | re.DOTALL
)

SCORE_HISTORY: Final = (
    "Score history (targets, attempts, drafts, critic notes, llm_results, evidence, det instances, llm_calls)"
)
# Each §20.1 row (its Data cell, verbatim) and the UI row that states it.
COVERED: Final[dict[str, str]] = {
    "CV file (original) and raw CV text": "cv_file",
    "Contact-stripped text": "stripped_text",
    (
        "Draft content (`draft_json`, `reviewed_text`, and the `output_json` of the draft's "
        "`extract_profile` calls)"
    ): "drafts",
    "Contact-stripped profile JSON": "profile",
    "Query proposals (`items`, and the `output_json` of their `propose_queries` calls)": "proposals",
    "Profile vectors": "proposals",
    SCORE_HISTORY: "score_history",
    "Superseded `det_results` instances": "det_superseded",
    "Feedback": "feedback",
    "Golden cases": "golden",
    "Eval runs, their case results and calls (golden, canary, schema dry-run)": "eval_runs",
    "Raw provider payload bodies": "raw_bodies",
    "`raw_items`": "raw_items",
    "`raw_responses` rows (metadata, body already nulled)": "raw_responses",
    (
        "Runs, run attempts, fetch requests, score cycles, coverage intervals, operations, bulk operations"
    ): "run_metadata",
    "Batch submissions (metadata, no content)": "batch_submissions",
    "ATS snapshots": "ats_snapshots",
    "Quota ledger, LLM spend ledger, `llm_budget`, `quota_windows`": "ledgers",
    "`llm_tuples`, `execution_configs`, `evaluation_policies`, `model_policies`": "ledgers",
    "Posting observations, jobs, revisions, lineage": "jobs",
    "Closed or stale jobs (not saved or applied, not referenced by an active golden case)": "closed_jobs",
    "Saved or applied jobs": "saved_jobs",
    "Embeddings": "embeddings",
    "LangGraph checkpoints": "checkpoints",
    "procrastinate finished jobs": "finished_jobs",
    "Idempotency records": "finished_jobs",
    "Logs": "logs",
    "Sessions": "sessions",
    "Login attempts": "sessions",
    "Login lockouts": "sessions",
    "Alerts": "alerts",
    "Exports": "exports",
    "Backups": "backups",
    "Backup sets, backup runs": "backup_records",
    "Deletion manifests, shipments, purge fences": "manifests",
    "LangSmith traces": "traces",
}

# Periods a row's UI text leaves out, with the reason: the 29 days are the batch reconciler's clock for a
# released submission (when it counts as resolved), a condition of the rule, not how long the data is kept.
EXEMPT: Final[dict[str, set[str]]] = {
    SCORE_HISTORY: {"29 days"},
    "Batch submissions (metadata, no content)": {"29 days"},
}

PERIOD: Final = re.compile(r"\b(\d+) (day|days|month|months)\b")
GERMAN_UNIT: Final = {"day": "Tag", "days": "Tage", "month": "Monat", "months": "Monate"}
INDEFINITELY: Final = {"en": "indefinitely", "de": "unbegrenzt"}


def spec_rows() -> dict[str, str]:
    """§20.1's rows: each Data cell with its Retention cell."""
    lines = SPEC.read_text(encoding="utf-8").splitlines()
    start = lines.index(HEADING)
    table: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("|"):
            table.append(line)
        elif table:
            break
    rows: dict[str, str] = {}
    for line in table[2:]:  # the header and its rule
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == 4, line
        rows[cells[0]] = cells[2]
    return rows


def ui_rows() -> list[str]:
    found = ROWS_DECLARATION.findall(SECTION.read_text(encoding="utf-8"))
    assert len(found) == 1, f"one `export const RETENTION_ROWS = [...] as const;` in {SECTION.name}"
    return re.findall(r'"([a-z_]+)"', found[0])


def catalogue(lang: str) -> dict[str, str]:
    loaded = json.loads(CATALOGUES[lang].read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def row_text(lang: str, row: str) -> str:
    table = catalogue(lang)
    return f"{table.get(f'settings_privacy.row.{row}', '')} {table.get(f'settings_privacy.kept.{row}', '')}"


def test_the_spec_table_is_found() -> None:
    rows = spec_rows()
    assert len(rows) >= 30, sorted(rows)
    assert "Backup sets, backup runs" in rows


def test_every_spec_row_has_a_ui_row_and_every_ui_row_a_spec_row() -> None:
    rows = spec_rows()
    shown = ui_rows()
    assert sorted(set(rows) - set(COVERED)) == [], "a §20.1 row the page does not state"
    assert sorted(set(COVERED) - set(rows)) == [], "a mapped row §20.1 no longer has"
    assert sorted(set(COVERED.values()) - set(shown)) == [], "a mapped UI row the page does not render"
    assert sorted(set(shown) - set(COVERED.values())) == [], "a UI row that states no §20.1 row"
    assert len(shown) == len(set(shown)), shown


@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_ui_row_has_its_strings(lang: str) -> None:
    table = catalogue(lang)
    for row in ui_rows():
        for part in ("row", "kept"):
            key = f"settings_privacy.{part}.{row}"
            assert table.get(key, "").strip(), f"{lang}: {key}"


@pytest.mark.parametrize("lang", ["en", "de"])
def test_each_ui_row_states_its_spec_rows_periods(lang: str) -> None:
    missing: list[str] = []
    for spec_row, retention in spec_rows().items():
        text = row_text(lang, COVERED[spec_row])
        for count, unit in PERIOD.findall(retention):
            period = f"{count} {unit}"
            if period in EXEMPT.get(spec_row, set()):
                continue
            wanted = period if lang == "en" else f"{count} {GERMAN_UNIT[unit]}"
            if wanted not in text:
                missing.append(f"{spec_row!r} ({period}): {wanted!r} not in {COVERED[spec_row]}")
        if "indefinitely" in retention.lower() and INDEFINITELY[lang] not in text.lower():
            missing.append(f"{spec_row!r}: {INDEFINITELY[lang]!r} not in {COVERED[spec_row]}")
    assert not missing, "\n".join(missing)


# The Logs row: rotation by size, as §20.1 says, never a bare time (final re-review R-M2, N7; final fix C5).
LOGS_KEPT: Final = {
    "en": "Rotated by size; about 14 days at normal volume",
    "de": "Rotation nach Größe; etwa 14 Tage bei normalem Aufkommen",
}


@pytest.mark.parametrize("lang", ["en", "de"])
def test_the_logs_row_says_they_rotate_by_size(lang: str) -> None:
    assert "Rotated by size" in spec_rows()["Logs"]
    assert catalogue(lang)["settings_privacy.kept.logs"] == LOGS_KEPT[lang]
