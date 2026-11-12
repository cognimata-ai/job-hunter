"""The alert catalogue as data (spec §17.6 L5631-5698; plan M5 Task 2, O-ops §2.2, Deviations 7 and 8).

- Every alert code the code base raises is catalogued: the codes of every `raise_alert`/`open_alert` call,
  collected by `tests/unit/test_i18n_codes.py`'s walk (`alert_codes`, which also resolves the computed sites),
  and the value of every module-level `*_ALERT` or `*_CODE` constant that is an alert code. So an uncatalogued
  code, which `is_manual` treats as manual, is one nobody raises.
- Every §17.6 row is held as the spec states it: its severity, its escalation (`warn / critical`), whether
  it is pushed (`warn (in-app only)` is not) and whether it is manual (`manual`, `manual acknowledgement`),
  parsed from the spec's table; `embedder.checksum_mismatch` resolves itself (Deviation 8, which spec
  revision 3.7 states).
- The rows beyond revision 3.6's table: the eight codes the code raised without a row and `docparse.leftover`,
  which revision 3.7 adds to §17.6 (Deviation 7).
- Every catalogued code has its `alert.<code>` text in English and in German.
- `push_priority` maps a severity to its ntfy priority (§17.6 L5631), and an entry is pushed by it unless its
  code is never pushed.
"""

import ast
import importlib
import json
import re
from pathlib import Path
from typing import Final, get_args

import pytest
from jobhunter.observability.catalogue import (
    CATALOGUE,
    NEVER_PUSHED,
    RESOLVE_ON_ACK,
    SEVERITIES,
    CatalogueEntry,
    Severity,
    is_manual,
    push_priority,
)

from tests.unit.test_i18n_codes import alert_codes, sources

REPO: Final = Path(__file__).resolve().parents[2]
SPEC: Final = REPO / "docs/superpowers/specs/2026-10-16-job-hunter-design.md"
HEADING: Final = "### 17.6 Alert catalogue"
TEXTS: Final = {lang: REPO / f"frontend/src/i18n/{lang}.json" for lang in ("en", "de")}

# A module constant that may hold an alert code; its value must also have the code's shape, since `*_CODE`
# also names exit codes, patterns and the codes of other catalogues (`UNSETTLED_CODE = "unsettled"`).
CODE_CONSTANT: Final = re.compile(r"_(ALERT|CODE)$")
ALERT_CODE: Final = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
# A §17.6 row: | `code` | severity | condition | auto-resolves |
SPEC_ROW: Final = re.compile(r"^\| `([a-z0-9_.]+)` \| ([^|]+) \| .* \| ([^|]+) \|$")
MANUAL_CELLS: Final = frozenset({"manual", "manual acknowledgement"})


def module_constants() -> set[str]:
    """The alert codes held by a module-level `*_ALERT` or `*_CODE` constant of `backend/src`."""
    found: set[str] = set()
    for module, tree in sources():
        names = [
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign | ast.AnnAssign)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name) and CODE_CONSTANT.search(target.id)
        ]
        if not names:
            continue
        loaded = importlib.import_module(module)
        for name in names:
            value = getattr(loaded, name)
            if isinstance(value, str) and ALERT_CODE.match(value):
                found.add(value)
    return found


def spec_rows() -> dict[str, tuple[str, str]]:
    """§17.6's rows: each code with its severity cell and its auto-resolves cell, stripped."""
    lines = SPEC.read_text(encoding="utf-8").splitlines()
    start = lines.index(HEADING)
    end = next(n for n in range(start + 1, len(lines)) if lines[n].startswith("**ntfy delivery.**"))
    rows: dict[str, tuple[str, str]] = {}
    for line in lines[start:end]:
        if match := SPEC_ROW.match(line):
            code, severity, resolves = match.groups()
            assert code not in rows, code
            rows[code] = (severity.strip(), resolves.strip())
    return rows


def test_every_raised_code_is_catalogued() -> None:
    raised = alert_codes()
    constants = module_constants()
    # the walks reach the code base: the gateway's constants and the computed sites' codes
    assert {"llm.unexpected_stop_reason", "run.failed", "source.breaker_open"} <= raised
    assert {"auth.lockout", "reconciler.failed", "api.unhandled_exception"} <= constants
    assert sorted((raised | constants) - set(CATALOGUE)) == []


def test_the_constant_walk_skips_what_is_no_alert_code() -> None:
    constants = module_constants()
    assert "unsettled" not in constants  # `spend_ledger.UNSETTLED_CODE`, a settlement code
    assert "embedder_unavailable" not in constants  # `profile.vectors.REQUEUE_CODE`, a requeue reason


def test_spec_rows_are_held_verbatim() -> None:
    assert CATALOGUE["run.failed"] == CatalogueEntry(
        code="run.failed", severity="critical", escalates_to=None, auto_resolves=True, pushed=True
    )
    assert CATALOGUE["quota.limit_breach"] == CatalogueEntry(
        code="quota.limit_breach", severity="critical", escalates_to=None, auto_resolves=False, pushed=True
    )
    for code in ("backup.stale", "system.disk_pressure"):
        assert CATALOGUE[code] == CatalogueEntry(
            code=code, severity="warn", escalates_to="critical", auto_resolves=True, pushed=True
        )
    assert CATALOGUE["notify.delivery_failed"] == CatalogueEntry(
        code="notify.delivery_failed", severity="warn", escalates_to=None, auto_resolves=True, pushed=False
    )
    assert CATALOGUE["dedup.field_conflict"].severity == "info"
    assert CATALOGUE["dedup.field_conflict"].pushed is False
    assert CATALOGUE["embedder.checksum_mismatch"].auto_resolves is True  # Deviation 8


def test_every_spec_row_is_held_verbatim() -> None:
    rows = spec_rows()
    assert len(rows) == 72  # revision 3.6's 63 rows and the nine of revision 3.7 (Deviation 7)
    for code, (severity_cell, resolves_cell) in rows.items():
        entry = CATALOGUE[code]
        severity, _, escalation = severity_cell.partition(" / ")
        in_app_only = severity.endswith(" (in-app only)")
        severity = severity.removesuffix(" (in-app only)")
        assert (entry.code, entry.severity, entry.escalates_to) == (code, severity, escalation or None), code
        assert entry.pushed is (severity != "info" and not in_app_only), code
        assert entry.auto_resolves is (resolves_cell not in MANUAL_CELLS), (code, resolves_cell)


def test_the_rows_beyond_the_spec() -> None:
    def entry(code: str, severity: Severity, *, auto: bool) -> CatalogueEntry:
        return CatalogueEntry(
            code=code, severity=severity, escalates_to=None, auto_resolves=auto, pushed=severity != "info"
        )

    beyond = {
        "llm.unexpected_stop_reason": entry("llm.unexpected_stop_reason", "warn", auto=False),
        "profile.extract_generation_mismatch": entry(
            "profile.extract_generation_mismatch", "warn", auto=False
        ),
        "dedup.ambiguous_grouping": entry("dedup.ambiguous_grouping", "info", auto=True),
        "quota.serpapi_renewal_stale": entry("quota.serpapi_renewal_stale", "warn", auto=True),
        "reconciler.failed": entry("reconciler.failed", "warn", auto=True),
        "retention.failed": entry("retention.failed", "warn", auto=True),
        "api.unhandled_exception": entry("api.unhandled_exception", "warn", auto=False),
        "auth.totp_key_mismatch": entry("auth.totp_key_mismatch", "critical", auto=False),
        "docparse.leftover": entry("docparse.leftover", "warn", auto=True),
    }
    assert {code: CATALOGUE[code] for code in beyond} == beyond
    assert set(CATALOGUE) == set(spec_rows()) | set(beyond)


def test_every_catalogue_code_has_en_and_de_text() -> None:
    for lang, path in TEXTS.items():
        texts = json.loads(path.read_text(encoding="utf-8"))
        missing = [code for code in CATALOGUE if not texts.get(f"alert.{code}", "").strip()]
        assert missing == [], lang


def test_push_priority() -> None:
    assert [push_priority(severity) for severity in SEVERITIES] == [None, 3, 5]
    assert SEVERITIES == get_args(Severity)  # info < warn < critical, the escalation's order


def test_an_entry_is_pushed_by_its_severity_unless_it_is_never_pushed() -> None:
    assert NEVER_PUSHED == {"notify.delivery_failed"}
    for code, entry in CATALOGUE.items():
        assert entry.code == code
        assert entry.pushed is (push_priority(entry.severity) is not None and code not in NEVER_PUSHED), code


def test_an_escalation_ranks_above_its_severity() -> None:
    escalating = {code: entry for code, entry in CATALOGUE.items() if entry.escalates_to is not None}
    assert set(escalating) == {"backup.stale", "system.disk_pressure"}
    for entry in escalating.values():
        assert entry.escalates_to is not None
        assert SEVERITIES.index(entry.escalates_to) > SEVERITIES.index(entry.severity)


@pytest.mark.parametrize(
    ("code", "manual"),
    [
        ("quota.limit_breach", True),
        ("llm.reservation_bound_violated", True),  # resolved by its acknowledgement
        ("run.failed", False),
        ("llm.auth_failed", False),  # a successful call resolves it
        ("x.uncatalogued", True),
    ],
)
def test_is_manual(code: str, manual: bool) -> None:
    assert is_manual(code) is manual


def test_an_alert_resolved_on_acknowledgement_is_manual() -> None:
    assert RESOLVE_ON_ACK == {"llm.reservation_bound_violated"}
    assert all(is_manual(code) for code in RESOLVE_ON_ACK)
