"""Revisions 3.5 and 3.6 of the design spec (M3 Task 25, M4 Task 35) agree with the code they describe.

The spec is prose, so only the values that the code also holds are pinned here:
- the status line names revision 3.6 and keeps the text of revisions 3.3 to 3.5;
- §11.5's JSON block is the generator wire schema of prompt-schema version 2, candidate C of spike V4b
  (`scripts/spikes/generator_v2_candidates.candidate_c`), compared as canonical JSON;
- §6.8's dry-run divides by the Arbeitnow page yield of `quota/dryrun.py` (V8's 17.5 h, Task 24);
- §11.3's anchor of a mandatory language with an `unspecified` level is the sentence of rubric v2 (R13);
- §7.16.6 releases a `submitted` batch whose retrieval keeps failing (U3), and its edges are those
  `db.transitions` allows a batch submission;
- §14.2 lists the routes M4 added, each a route of the api's OpenAPI document (`create_app().api.openapi()`);
- §14.4's codes are problem codes (`errors.ErrorCode`), `totp_required` among them;
- §4.1's frontend rows hold `frontend/package.json`'s exact pins, the M4 additions among them;
- §13.1's range of `scoring.batch_min_targets` is the settings model's (20 to 100,000);
- §15.7's confirm modal states what `profile.estimate` computes for its ads at §21.4's spend (209 in the
  batch, ~971 online);
- §15.4's drawer offers no "Apply link".
"""

# ruff: noqa: RUF001  (the spec's text as it is written: its dashes and its prime)

import importlib.util
import json
import re
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import ModuleType
from typing import get_args

from jobhunter.api.app import create_app
from jobhunter.db.seed import DEFAULT_PROJECTED_CRITIC_RATE, load_settings_v1
from jobhunter.db.transitions import TRANSITIONS
from jobhunter.errors import ErrorCode
from jobhunter.llm import budget
from jobhunter.llm.budget import BudgetParams
from jobhunter.llm.fake import canonical_schema_json
from jobhunter.profile.estimate import estimate_for
from jobhunter.quota.dryrun import ARBEITNOW_HOURS_PER_PAGE
from jobhunter.scoring.rubric import RUBRIC_V2, load_rubric
from jobhunter.settings.llm_projection import central_cost_per_target
from jobhunter.settings.model import Scoring, Settings

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "docs/superpowers/specs/2026-10-16-job-hunter-design.md"
PACKAGE_JSON = REPO / "frontend/package.json"


def _spec_lines() -> list[str]:
    return SPEC.read_text(encoding="utf-8").splitlines()


def _section(heading: str) -> list[str]:
    """The lines of the section that starts with `heading`, up to the next heading of its level or above."""
    lines = _spec_lines()
    starts = [i for i, line in enumerate(lines) if line.startswith(heading)]
    assert len(starts) == 1, f"{heading!r} heads {len(starts)} sections"
    level = len(heading) - len(heading.lstrip("#"))
    body: list[str] = []
    for line in lines[starts[0] + 1 :]:
        marks = len(line) - len(line.lstrip("#"))
        if 0 < marks <= level and line[marks : marks + 1] == " ":
            break
        body.append(line)
    return body


def _candidates() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "generator_v2_candidates", REPO / "scripts/spikes/generator_v2_candidates.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Revision 3.4's status text after its "**Approved** — revision 3.4. " head: the 3.3 and 3.4 entries.
EARLIER_REVISIONS = (
    "Revision 3.3 was approved by the user on 2026-10-16, after council approval by Codex GPT-6 Astra and"
    " Claude Fable 5.1 in six rounds of 4-eyes review, with the chair's decisions on three items (§2.4)."
    " Revision 3.4 (2026-10-21, the user's decision after spike V19): the default"
    " `scoring.batch_max_items` is 500 instead of 1,000, so the $2.00 overrun buffer covers the measured"
    " prompt overhead (§6.9.1)."
)
# Revision 3.5's entry, kept as it was written.
REVISION_3_5 = (
    "Revision 3.5 (2026-10-29, the user's decisions U1–U4 and the M3 plan's deviations): the generator"
    " output schema of §11.5 is candidate C of spike V4b (generic fact items plus generic criterion"
    " items; the §11.5 schema of revision 3.4 is rejected by the API as too large), with §11.4, §11.6,"
    " §10.6, §10.11 and §11.9 adjusted to it; §11.3's language anchor follows §11.4 (B2 or higher is"
    " `met`); §6.8's Arbeitnow page yield is V8's 17.5 h, so the coverage-risk warning fires for"
    " intervals above 35 h (a 7-day interval raises it); `display.timezone` is a `schedule` field"
    " (§13.1); and the M3 plan's recorded deviations (its Deviations 1–41) on §3.3.3, §6.9.3, §7.11–7.13,"
    " §7.16, §7.18, §9.2, §9.3, §9.6, §9.7, §10.6, §10.10, §10.11, §11.6, §11.8, §11.13, §13.1, §14.2,"
    " §17.2, §17.6, §18.6, §20.1, §21.4 and §22, with the M3 execution rulings T3-b (§11.4, §11.6), T4-a"
    " (§7.3), T5-e (§11.6), T9-f (§11.7.2), T11-f and T11-g (§10.4), T12-b (§9.6.2), T15-a and T15-d"
    " (§8.7), T16-a, T16-d, T16-f, T16-g and T17-a (§10.10, §17.4, §17.6), T18-a (§17.5), T19-c (§7.4),"
    " T20-a (§13.2), T20-d (§10.3, §11.13), T22-c and T22-d′ (§8.6.1, §8.6.2), T22-e (§9.7) and M2-C1"
    " (§12.3), and the overhead of V19's M3 re-count, 1,878 tokens (§6.9.1)."
)


def test_the_status_line_names_revision_3_6_and_keeps_the_earlier_revisions() -> None:
    status = [line for line in _spec_lines() if line.startswith("| Status |")]
    assert len(status) == 1
    head = f"| Status | **Approved** — revision 3.6. {EARLIER_REVISIONS} {REVISION_3_5} Revision 3.6 ("
    assert status[0].startswith(head)
    assert status[0].endswith(" Audit trail: `docs/council/` |")
    open_points = [line for line in _spec_lines() if line.startswith("| Known open points ")]
    assert len(open_points) == 1
    assert open_points[0].startswith("| Known open points (revision 3.6) |")


def test_the_generator_schema_of_11_5_is_candidate_c() -> None:
    body = _section("### 11.5 ")
    opens = [i for i, line in enumerate(body) if line == "```json"]
    assert len(opens) == 1, "§11.5 holds exactly one JSON block"
    close = body.index("```", opens[0] + 1)
    block = json.loads("\n".join(body[opens[0] + 1 : close]))
    candidate = _candidates().candidate_c()
    assert canonical_schema_json(block) == canonical_schema_json(candidate)
    # The key order too (`json.loads` keeps it): the model emits the properties in the schema's order.
    assert json.dumps(block) == json.dumps(candidate)


def test_the_arbeitnow_page_yield_of_6_8_is_the_dry_run_constant() -> None:
    divisors = [
        float(match.group(1))
        for line in _section("### 6.8 ")
        if (match := re.search(r"expected_pages = proposed\.interval_h / ([0-9]+(?:\.[0-9]+)?) \+ 1", line))
    ]
    assert divisors == [ARBEITNOW_HOURS_PER_PAGE]


def test_the_language_anchor_of_11_3_is_rubric_v2s_sentence() -> None:
    languages = load_rubric(RUBRIC_V2).anchors["languages"]
    assert isinstance(languages, dict)
    sentence = languages["unspecified_level"]
    assert isinstance(sentence, str)
    head = "A mandatory language with an `unspecified` level"
    assert sentence.startswith(head)
    paragraphs = [line for line in _section("### 11.3 ") if head in line]
    assert len(paragraphs) == 1
    assert paragraphs[0][paragraphs[0].index(head) :] == sentence


# ---- revision 3.6 (M4) -------------------------------------------------------------------------------------

# U3: the release of a lost batch, inserted after the quarantine's release (§7.16.6).
QUARANTINE_RELEASE = (
    "submit_ambiguous ──explicit user release──► released"
    "     (in_batch targets → pending; spend stays ambiguous)"
)
LOST_BATCH_RELEASE = (
    "submitted ──explicit user release while batch.retrieve_failing is open and now > expires_at + 1 h"
    "──► released     (in_batch targets → pending; spend stays ambiguous; provider_batch_id kept)"
)
# An edge of a state machine block (whitespace collapsed), `a | b ──label──► c`; a target that is no state
# ("(same state; …)") is none.
EDGE = re.compile(r"^([a-z_]+(?: \| [a-z_]+)*) ──.*──► ([a-z_]+)\b")


def _collapsed(line: str) -> str:
    """`line` with every run of whitespace one space, and none at its ends: its alignment is no content."""
    return " ".join(line.split())


def test_7_16_6_releases_a_lost_batch_and_its_edges_are_the_transition_table_s() -> None:
    body = [_collapsed(line) for line in _section("#### 7.16.6 ")]
    lost, quarantine = _collapsed(LOST_BATCH_RELEASE), _collapsed(QUARANTINE_RELEASE)
    assert body.count(lost) == 1
    assert body[body.index(quarantine) + 1] == lost
    edges: set[tuple[str, str]] = set()
    for line in body:
        if (match := EDGE.match(line)) is not None:
            target = match.group(2)
            edges |= {(source, target) for source in match.group(1).split(" | ") if source != target}
    assert edges == TRANSITIONS["batch_submissions"].allowed


# The routes M4 added that §14.2 lists (a path parameter of any name is `{}`).
M4_ROUTES = frozenset(
    {
        ("GET", "/api/system/status"),
        ("GET", "/api/queries"),
        ("GET", "/api/watchlist"),
        ("GET", "/api/eval-runs"),
        ("GET", "/api/jobs/facets"),
        ("GET", "/api/settings/rubric-versions/{}/activation-preview"),
        ("GET", "/api/settings/prompt-schema-versions/{}/activation-preview"),
    }
)
ROUTE_ROW = re.compile(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`\s*\|")
ALTERNATIVES = re.compile(r"\{([^{},]+(?:,[^{},]+)+)\}")  # `{rubric,prompt-schema}`, never a parameter
PARAMETER = re.compile(r"\{[^{}]*\}")


def _paths(path: str) -> set[str]:
    """The paths a §14.2 path cell names: its `{a,b}` alternatives expanded, each parameter `{}`."""
    match = ALTERNATIVES.search(path)
    if match is None:
        return {PARAMETER.sub("{}", path)}
    return set().union(
        *(
            _paths(path[: match.start()] + choice + path[match.end() :])
            for choice in match.group(1).split(",")
        )
    )


def test_14_2_lists_the_routes_m4_added() -> None:
    listed = {
        (match.group(1), path)
        for line in _section("### 14.2 ")
        if (match := ROUTE_ROW.match(line)) is not None
        for path in _paths(match.group(2))
    }
    assert sorted(M4_ROUTES - listed) == []
    document = create_app().api.openapi()
    served = {
        (method.upper(), PARAMETER.sub("{}", path))
        for path, ops in document["paths"].items()
        for method in ops
    }
    assert sorted(M4_ROUTES - served) == []


def test_14_4_codes_are_problem_codes_and_totp_required_is_one() -> None:
    lists = [line for line in _section("### 14.4 ") if line.startswith("- Stable `code` values include:")]
    assert len(lists) == 1
    codes = re.findall(r"`([a-z_]+)`", lists[0].split(":", 1)[1])
    assert "totp_required" in codes
    assert sorted(set(codes) - set(get_args(ErrorCode))) == []


# §4.1's names of npm packages that are no package name; every other frontend row names its package.
COMPONENT_PACKAGES = {"Vite": "vite", "React": "react", "TypeScript": "typescript"}
# The pins M4 added (i18n, the Radix primitives, Testing Library, jsdom, axe, the E2E suite).
M4_FRONTEND = frozenset(
    {
        "i18next",
        "react-i18next",
        "@radix-ui/react-checkbox",
        "@radix-ui/react-dismissable-layer",
        "@radix-ui/react-focus-scope",
        "@radix-ui/react-popover",
        "@radix-ui/react-portal",
        "@radix-ui/react-slider",
        "@radix-ui/react-tabs",
        "@testing-library/dom",
        "@testing-library/react",
        "@testing-library/user-event",
        "jsdom",
        "@axe-core/playwright",
        "@playwright/test",
    }
)
SEMVER = re.compile(r"\d+\.\d+\.\d+")


def test_4_1_frontend_rows_hold_the_package_json_pins() -> None:
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    pins: dict[str, str] = {**package["dependencies"], **package["devDependencies"]}
    assert all(SEMVER.fullmatch(pin) for pin in pins.values()), "exact pins only"
    body = _section("### 4.1 ")
    rows: dict[str, str] = {}
    for line in body:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not line.startswith("|") or len(cells) != 3:
            continue
        name = COMPONENT_PACKAGES.get(cells[1], cells[1])
        if name in pins and (version := SEMVER.search(cells[2])) is not None:
            assert name not in rows, f"{name} has two rows"
            rows[name] = version.group(0)
    assert sorted(M4_FRONTEND - set(rows)) == []
    assert rows == {name: pins[name] for name in rows}
    assert not any("1.64.1" in line for line in body), "the E2E suite is @playwright/test"


def test_13_1_range_of_batch_min_targets_is_the_settings_model_s() -> None:
    table = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in _section("### 13.1 ")
        if line.startswith("|")
    ]
    rows = [cells for cells in table if len(cells) > 1 and cells[1] == "`scoring.batch_min_targets`"]
    assert len(rows) == 1
    cells = rows[0]
    field = Scoring.model_fields["batch_min_targets"]
    low = next(item.ge for item in field.metadata if hasattr(item, "ge"))
    high = next(item.le for item in field.metadata if hasattr(item, "le"))
    assert (low, high) == (20, 100_000)
    assert cells[2:4] == [f"int, {low:,}–{high:,}", f"`{field.default}`"]


# §15.7's confirm modal: the ads, the batch allocation, the batch's ads and the online rest.
MODAL = re.compile(
    r"re-scores\s+([\d,]+)\s+retained\s+ads\.\s+Batch\s+allocation\s+\$([\d.]+)\s.*?"
    r"about\s+(\d+)\s+ads\s+in\s+one\s+batch.*?the\s+other\s+~([\d,]+)\s+are\s+scored\s+online",
    re.DOTALL,
)


def test_the_confirm_modal_of_15_7_states_the_estimate_of_21_4_s_second_case() -> None:
    """§21.4's second case, $38.20 counted ($37.00 of it scoring): the estimate's split of the modal's ads."""
    match = MODAL.search("\n".join(_section("### 15.7 ")))
    assert match is not None
    ads, allocation = int(match.group(1).replace(",", "")), Decimal(match.group(2))
    batch, online = int(match.group(3)), int(match.group(4).replace(",", ""))
    assert (batch, online) == (209, 971)
    settings = Settings.model_validate(load_settings_v1())
    policy = settings.scoring.model_policy
    now = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)
    row = budget.new_row(date(2026, 10, 1), BudgetParams.from_payload(settings.to_payload()), now)
    row.counted_usd, row.scoring_counted_usd, row.nonscoring_counted_usd = (
        Decimal("38.20"),
        Decimal("37.00"),
        Decimal("1.20"),
    )
    # `bulk.hold_per_item` of the seeded evaluation policy, whose projected critic rate is the central one
    hold = (DEFAULT_PROJECTED_CRITIC_RATE * budget.c_hat_followup(policy)).quantize(
        budget.USD_QUANTUM, rounding=ROUND_HALF_UP
    )
    estimate = estimate_for(
        settings,
        targets=ads,
        row=row,
        c_hat=central_cost_per_target(policy),
        c_hat_source="central",
        hold_per_item_usd=hold,
        next_version_no=5,
    )
    assert estimate.bulk_budget_usd == allocation
    assert (estimate.batch_targets, estimate.online_targets) == (batch, online)


def test_the_drawer_of_15_4_offers_no_apply_link() -> None:
    body = "\n".join(_section("### 15.4 "))
    assert "Apply link" not in body
    assert "[Open ad ↗]" in body
