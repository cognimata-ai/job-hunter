"""`frontend/src/i18n/codes.json` (plan M4 Task 20): the server code catalogues the SPA translates (D39).

Each namespace equals its backend source, derived from the code and never typed out here: a Literal's
arguments (`typing.get_args`), a mapping's keys, or an AST walk of the calls that pass a code. A failure names
the missing and the extra codes. The catalogue's own form is pinned too: exactly these namespaces, each a
sorted list of distinct strings (the frontend's `i18n.test.ts` checks that both languages have a key for each
code), and beside them `order`: the namespaces whose codes the UI lists in their backend's own order (the
seniority ladder, plan M4 Task 23, Ruling T23-c; Needs attention's sections, plan M4 Task 31), each equal to
that order; `order.effort`, each model's supported efforts in `Effort`'s order
(`llm.pricing.SUPPORTED_EFFORTS`, plan M4 Task 30, resolution c): the effort options of the model grid, shown
as their codes, so they are no namespace; and `order.retryable_target_state`, the target states the retry
route takes back (plan M4 Task 31).

The alert codes are the `code` of every `raise_alert` and `open_alert` call in `backend/src`, with a module
constant resolved by importing its module, and the codes of a module's wrapper that forwards its own `code`
parameter to one of them (`scoring.reconcilers._opened`). A call whose code is computed (a loop variable, a
mapping's value) is one of `DYNAMIC_SITES`, which derives that site's codes from the object the code comes
from; a computed call not listed there fails the test, as does a listed site the walk no longer finds.
"""

import ast
import importlib
import json
import re
import textwrap
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from types import UnionType
from typing import Any, Final, Literal, TypeAliasType, Union, get_args, get_origin, get_type_hints

import pytest
from jobhunter.api.routes import profile_review, spend
from jobhunter.api.routes import settings as settings_routes
from jobhunter.db import ranking
from jobhunter.db.seed import SOURCES
from jobhunter.errors import ErrorCode
from jobhunter.graphs.collect.nodes.finalize import PartialCause
from jobhunter.graphs.profile.nodes import llm_outcome
from jobhunter.graphs.profile.nodes import propose_queries as propose_node
from jobhunter.jobs.cards import Badge, ScoreOut
from jobhunter.jobs.detail import DET_CRITERIA, DetailScoreOut
from jobhunter.jobs.query import EMPLOYMENT_VALUES, SENIORITY_VALUES
from jobhunter.llm import gateway, pricing, schema_dry_run
from jobhunter.llm.schemas import generator
from jobhunter.llm.schemas import profile as profile_schema
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.published import KnockoutCode
from jobhunter.normalize.titles import SENIORITY_LADDER
from jobhunter.observability import attention, health
from jobhunter.observability import catalogue as alert_catalogue
from jobhunter.observability.health import RunStatus
from jobhunter.observability.lists import AlertStatus, CycleStatus
from jobhunter.observability.status import BannerCode, Pace, PaidSearchReason
from jobhunter.profile import review
from jobhunter.profile.contacts import Category
from jobhunter.profile.errors import RejectReason
from jobhunter.profile.review import Repair
from jobhunter.queries import authoring
from jobhunter.quota import windows
from jobhunter.quota.windows import WindowState
from jobhunter.scoring import aggregate, eval_runs, targets, validator
from jobhunter.scoring.cycles import CycleTrigger, PauseReason
from jobhunter.scoring.det_criteria import DetCriterion
from jobhunter.scoring.filters import FilterCode
from jobhunter.scoring.golden import CaseSource
from jobhunter.scoring.golden import Label as FeedbackLabel
from jobhunter.scoring.knockouts import DEGREE_RANK
from jobhunter.scoring.routing import DerivedOutcome
from jobhunter.scoring.rubric import LLM_CRITERIA
from jobhunter.scoring.steps import FACT_REJECTED, FACT_UNCERTAIN, FACT_UNREVIEWED
from jobhunter.scoring.triggers import Trigger
from jobhunter.settings import model as settings_model
from jobhunter.settings.effects import EffectClass
from jobhunter.settings.llm_projection import CHatSource
from jobhunter.settings.validate import GateState
from jobhunter.sources import breaker, watchlist_listing, watchlist_patterns
from jobhunter.sources.parsing.dates import Precision
from jobhunter.sources.parsing.salary import Period
from jobhunter.worker import operations, runs
from jobhunter.worker.tasks import QUEUES
from procrastinate.jobs import Status as QueueJobStatus

REPO: Final = Path(__file__).resolve().parents[2]
SRC: Final = REPO / "backend/src"
PACKAGE: Final = SRC / "jobhunter"
CODES: Final = REPO / "frontend/src/i18n/codes.json"
BASELINE_MIGRATION: Final = PACKAGE / "db/migrations/versions/0001_m0_baseline.py"
COLLECTION_MIGRATION: Final = PACKAGE / "db/migrations/versions/0005_m1_collection.py"
ALERT_FUNCTIONS: Final = frozenset({"raise_alert", "open_alert"})


def literal_values(tp: object) -> set[str]:
    """The strings of a Literal, through unions and `type` aliases."""
    if isinstance(tp, TypeAliasType):
        return literal_values(tp.__value__)
    origin = get_origin(tp)
    if origin is Literal:
        values = get_args(tp)
        assert all(isinstance(v, str) for v in values), values
        return {str(v) for v in values}
    if origin is Union or origin is UnionType:
        return set().union(*(literal_values(arg) for arg in get_args(tp)))
    raise AssertionError(f"{tp!r} is no Literal of strings")


def module_of(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def sources() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(PACKAGE.rglob("*.py")):
        yield module_of(path), ast.parse(path.read_text(encoding="utf-8"), str(path))


def called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def local_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The names a function binds itself: its parameters and every name it stores."""
    args = function.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    names = {a.arg for a in params if a is not None}
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


Function = ast.FunctionDef | ast.AsyncFunctionDef


def calls_by_function(tree: ast.Module) -> Iterator[tuple[ast.Call, Function | None]]:
    """Every call of `tree` with its innermost enclosing function (`None` at module level)."""

    def visit(node: ast.AST, function: Function | None) -> Iterator[tuple[ast.Call, Function | None]]:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else function
            if isinstance(child, ast.Call):
                yield child, function
            yield from visit(child, inner)

    yield from visit(tree, None)


def resolve(module: str, expr: ast.expr, function: Function | None) -> str | None:
    """The string `expr` names: a literal, or a module constant (also `alias.NAME`); `None` when computed."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    bound = local_names(function) if function is not None else set()
    path: list[str] = []
    node: ast.expr = expr
    while isinstance(node, ast.Attribute):
        path.insert(0, node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id in bound:
        return None
    value: Any = importlib.import_module(module)
    for name in [node.id, *path]:
        value = getattr(value, name)
    assert isinstance(value, str), f"{module}: {ast.unparse(expr)} is {value!r}, not a code"
    return value


def constructor_codes(module: str, constructor: str) -> set[str]:
    """The `code=` of every `constructor(...)` call in `module`, resolved."""
    path = Path(str(importlib.import_module(module).__file__))
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    codes: set[str] = set()
    for call, function in calls_by_function(tree):
        expr = keyword(call, "code")
        if called_name(call) == constructor and expr is not None:
            code = resolve(module, expr, function)
            assert code is not None, f"{module}: {constructor}(code={ast.unparse(expr)}) is computed"
            codes.add(code)
    return codes


# The alert calls whose code is computed, by (module, function), each with the codes it can pass, derived from
# the object the code comes from.
DYNAMIC_SITES: Final[dict[tuple[str, str], Callable[[], set[str]]]] = {
    # `for code in fx.alerts`: an observation's alerts
    ("jobhunter.quota.ledger", "_record_effects"): lambda: literal_values(windows.ObservationAlert),
    # `code, severity = ALERTS[health]`
    ("jobhunter.sources.breaker", "record_failure"): lambda: {code for code, _ in breaker.ALERTS.values()},
    # `code, message = _END_ALERTS[status]`
    ("jobhunter.worker.runs", "_finalize"): lambda: {code for code, _ in runs._END_ALERTS.values()},
    # `alert.code` of a queued `CacheAlert`
    ("jobhunter.llm.cache_monitor", "write_alerts"): lambda: constructor_codes(
        "jobhunter.llm.cache_monitor", "CacheAlert"
    ),
}


def wrappers(tree: ast.Module) -> set[str]:
    """The module's functions that pass their own `code` parameter on to an alert function."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            params = {a.arg for a in [*node.args.args, *node.args.kwonlyargs]}
            for call in ast.walk(node):
                if (
                    "code" in params
                    and isinstance(call, ast.Call)
                    and called_name(call) in ALERT_FUNCTIONS
                    and isinstance(expr := keyword(call, "code"), ast.Name)
                    and expr.id == "code"
                ):
                    found.add(node.name)
    return found


def alert_codes(
    modules: Iterable[tuple[str, ast.Module]] | None = None,
    dynamic: Mapping[tuple[str, str], Callable[[], set[str]]] = DYNAMIC_SITES,
) -> set[str]:
    """The alert codes of `modules` (default: every module of `backend/src`).

    The forwarding wrappers are collected across all the modules first; a wrapper's callers are then found in
    any module, called by name (`wrapper(…)`) or through a module (`mod.wrapper(…)`), and each must pass a
    `code=` the walk resolves, as every alert call must, or be a `dynamic` site.
    """
    parsed = list(sources() if modules is None else modules)
    forwarding = {(module, name) for module, tree in parsed for name in wrappers(tree)}
    wrapper_names = {name for _, name in forwarding}
    codes: set[str] = set()
    computed: set[tuple[str, str]] = set()
    for module, tree in parsed:
        for call, function in calls_by_function(tree):
            name = called_name(call)
            alert_call = name in ALERT_FUNCTIONS
            if not alert_call and name not in wrapper_names:
                continue
            expr = keyword(call, "code")
            assert expr is not None, f"{module}:{call.lineno}: {name}() without code="
            own = function is not None and (module, function.name) in forwarding
            if alert_call and own and isinstance(expr, ast.Name) and expr.id == "code":
                continue  # a wrapper's own forwarding call: its callers pass the codes
            code = resolve(module, expr, function)
            if code is not None:
                codes.add(code)
                continue
            site = (module, function.name if function is not None else "<module>")
            assert site in dynamic, (
                f"{module}:{call.lineno}: {name}(code={ast.unparse(expr)}) is computed; add the site to "
                "DYNAMIC_SITES with the source of its codes"
            )
            computed.add(site)
            codes |= dynamic[site]()
    stale = set(dynamic) - computed
    assert not stale, f"DYNAMIC_SITES lists sites the walk no longer finds: {sorted(stale)}"
    return codes


WARNING_MODULES: Final = (*sorted((PACKAGE / "settings").rglob("*.py")), PACKAGE / "quota/dryrun.py")


def warning_codes(paths: tuple[Path, ...] = WARNING_MODULES) -> set[str]:
    """The code of every dry-run `Warning("…")` call in `paths` (`settings/` and `quota/dryrun.py`)."""
    codes: set[str] = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
            if isinstance(node, ast.Call) and called_name(node) == "Warning":
                first = node.args[0] if node.args else keyword(node, "code")
                assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                    f"{path}:{node.lineno}: a Warning whose code is not a literal"
                )
                codes.add(first.value)
    return codes


def issue_codes() -> set[str]:
    """The codes of a refused settings field and of a profile validation issue, as the api answers them."""
    return literal_values(settings_routes.FieldIssueOut.model_fields["code"].annotation) | literal_values(
        profile_review.ValidationIssueOut.model_fields["code"].annotation
    )


def degree_levels() -> set[str]:
    """The degree levels a knockout can show: the ad's (the generator's facts), the profile's education, and
    the levels the degree knockout compares (`scoring.knockouts.DEGREE_RANK`, its `profile_level`)."""
    ad = literal_values(generator.Degree.model_fields["level"].annotation)
    profile = literal_values(profile_schema.Education.model_fields["level"].annotation)
    return ad | profile | set(DEGREE_RANK)


def _check_in(text: str, column: str, where: str, *, not_null: bool = False) -> set[str]:
    """The values of the one `<column> text [NOT NULL] CHECK (<column> IN ('a','b',…))` of `text`."""
    null = r"NOT\s+NULL\s+" if not_null else ""
    found = re.findall(rf"\b{column}\s+text\s+{null}CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)\s*\)", text)
    assert len(found) == 1, f"{where}: {len(found)} CHECKs of {column}"
    values = re.findall(r"'([^']*)'", found[0])
    assert values, f"{where}: the CHECK of {column} lists no value"
    return set(values)


def check_values(path: Path, column: str) -> set[str]:
    """The values of a migration's `<column> text CHECK (<column> IN ('a','b',…))`, read from its text."""
    return _check_in(path.read_text(encoding="utf-8"), column, path.name)


def table_check_values(path: Path, table: str, column: str) -> set[str]:
    """The values of `<column> text NOT NULL CHECK (<column> IN (…))` in a migration's `CREATE TABLE <table>`
    (a column name that other tables of the migration use too, as `kind`)."""
    text = path.read_text(encoding="utf-8")
    tables = re.findall(rf"CREATE TABLE {table} \((.*?)\n\);", text, flags=re.DOTALL)
    assert len(tables) == 1, f"{path.name}: {len(tables)} CREATE TABLE {table}"
    return _check_in(tables[0], column, f"{path.name} {table}", not_null=True)


def operation_runner_codes() -> set[str]:
    """The literal `error_code=` of every call in `worker/operations.py`: the codes the operation runner
    itself ends or requeues an operation with (`no_handler`, `bug`, `already_executing`)."""
    path = Path(str(operations.__file__))
    codes: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Call) and isinstance(expr := keyword(node, "error_code"), ast.Constant):
            assert isinstance(expr.value, str), f"{path}:{node.lineno}: error_code={expr.value!r}"
            codes.add(expr.value)
    return codes


def draft_failure_codes() -> set[str]:
    """Every literal code a failed draft's `failure_code` can hold (plan M4 Task 27, Ruling T27-b): the
    document and model-outcome codes of `profile.review` and `low_text`; the extract node's other failures
    (`llm_outcome`'s mapping of the gateway's errors) and its requeue code, which the failure hook writes
    after the last attempt; and the hooks' own `attempts_exhausted` with the runner's codes.
    `llm_request_<status>` is computed, so the UI shows it with its generic text."""
    document = {*review.DOCUMENT_CODES, *review.MODEL_OUTCOME_CODES, review.LOW_TEXT}
    model = {code for _, code in llm_outcome._FAILURES} | {
        *llm_outcome.TERMINAL_CODES,
        llm_outcome.LLM_TRANSIENT,
    }
    return document | model | {operations.ATTEMPTS_EXHAUSTED} | operation_runner_codes()


def call_args(module: str, constructor: str) -> set[str]:
    """The first argument of every `constructor(...)` call in `module`, resolved; a computed one fails."""
    path = Path(str(importlib.import_module(module).__file__))
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    codes: set[str] = set()
    for call, function in calls_by_function(tree):
        if called_name(call) == constructor and call.args:
            code = resolve(module, call.args[0], function)
            assert code is not None, f"{module}: {constructor}({ast.unparse(call.args[0])}) is computed"
            codes.add(code)
    return codes


def operation_runner_failure_codes() -> set[str]:
    """The literal `error_code=` of every call in `worker/operations.py` with `status="failed"`: the codes the
    operation runner itself ends an operation `failed` with (`no_handler`). Its requeue codes (`bug`,
    `already_executing`) end one only through the attempts rule, whose code is `attempts_exhausted`."""
    path = Path(str(operations.__file__))
    codes: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if not isinstance(node, ast.Call):
            continue
        status, expr = keyword(node, "status"), keyword(node, "error_code")
        if isinstance(status, ast.Constant) and status.value == "failed" and isinstance(expr, ast.Constant):
            assert isinstance(expr.value, str), f"{path}:{node.lineno}: error_code={expr.value!r}"
            codes.add(expr.value)
    return codes


def proposal_failure_codes() -> set[str]:
    """Every literal code a failed proposal's `failure_code` can hold (plan M4 Task 29, resolution c): the
    `last_error_code` of its newest `propose_queries` operation (`queries.proposals.FAILURE_CODE_SQL`),
    written where the operation ends `failed`:
    - the node's own end (`graphs.profile.nodes.propose_queries`, `finish_in(status="failed", ...)`):
      `llm_outcome`'s mapping of the gateway's errors, the walk's terminal codes and the repair's
      `invalid_output`, and the node's own `Fail(…)` codes (`profile_unavailable`);
    - the attempts rule (`worker.operations._release`) of a requeue, a reclaim or the takeover sweep:
      `attempts_exhausted` (the requeue's own code, `llm_transient` among them, is not kept);
    - the runner's own failure (`no_handler`).
    `llm_request_<status>` is computed, so the UI shows it with its generic text."""
    terminal = {*llm_outcome.TERMINAL_CODES, gateway.INVALID_OUTPUT}
    model = {code for _, code in llm_outcome._FAILURES} | terminal
    node = call_args(propose_node.__name__, "Fail")
    return model | node | {operations.ATTEMPTS_EXHAUSTED} | operation_runner_failure_codes()


def basis_values() -> set[str]:
    """The basis of a criterion: an LLM criterion's (the generator's) and a deterministic one's."""
    llm = literal_values(generator.Criterion.model_fields["basis"].annotation)
    return llm | literal_values(get_type_hints(DetCriterion)["basis"])


# Each namespace of codes.json with its backend source.
NAMESPACES: Final[dict[str, Callable[[], set[str]]]] = {
    "problem": lambda: literal_values(ErrorCode),
    "pause": lambda: literal_values(PauseReason),
    "partial": lambda: literal_values(PartialCause),
    # RunStatus is the `runs.status` CHECK in its order (pinned to the database's by test_api_runs_list)
    "run_status": lambda: literal_values(RunStatus),
    "target_state": lambda: {state for state in ranking.STATE_BUCKET if state is not None},
    "draft_status": lambda: literal_values(profile_review.DraftStatus),
    "draft_repair": lambda: literal_values(Repair),
    "upload_reason": lambda: literal_values(RejectReason),
    "warning": warning_codes,
    "issue": issue_codes,
    "alert": alert_codes,
    "banner": lambda: literal_values(BannerCode),
    # the header's LLM pace and the reason of the paid-search pause (plan M4 Task 21)
    "pace": lambda: literal_values(Pace),
    "paid_search_reason": lambda: literal_values(PaidSearchReason),
    # the quota providers: their names are the product names, the same in both languages (plan M4 Task 21)
    "provider": lambda: literal_values(windows.Provider),
    "window_state": lambda: literal_values(WindowState),
    "instance_health": lambda: literal_values(breaker.Health),
    "alert_status": lambda: literal_values(AlertStatus),
    # the job cards (plan M4 Task 22): the card badges, the knockout codes, the hard-filter reasons, why a
    # duplicate group is not scorable (the `duplicate_groups.unscorable_reason` CHECK), the score's confidence
    # and the source ids, whose names are the product names, the same in both languages
    "badge": lambda: literal_values(Badge),
    "knockout": lambda: literal_values(KnockoutCode),
    "filter_reason": lambda: literal_values(FilterCode),
    "group_unscorable": lambda: check_values(COLLECTION_MIGRATION, "unscorable_reason"),
    "confidence": lambda: literal_values(ScoreOut.model_fields["confidence"].annotation),
    "source": lambda: {source.id for source in SOURCES},
    # the profile's side of a knockout (plan M4 Task 22, Ruling T22-e): `work_permit`'s work authorisation and
    # `degree`'s level, also the level of a degree requirement
    "work_authorization": lambda: literal_values(
        profile_schema.UserEntered.model_fields["work_authorization_de"].annotation
    ),
    "degree_level": degree_levels,
    # the Jobs list's Employment and Seniority filters (plan M4 Task 23), also a `contract_type` knockout's
    # employment types: the employment types plus `unknown`, and the seniority ladder
    "employment": lambda: set(EMPLOYMENT_VALUES),
    "seniority": lambda: set(SENIORITY_VALUES),
    # the detail drawer (plan M4 Task 24, resolution g): the criteria (§11.2's eight), their basis; the
    # posted date's precision and the salary's period; the result's status and its critic outcome; a critic
    # round's triggers, verdict, derived outcome and issues (a grounded dispute's assessment, or a required
    # fact it did not confirm, `jobs.detail._issues`); the caps (a binding knockout's code, or the industry
    # penalty) and their kind, the industry preference, the profile section of an evidence row, a merge
    # event's kind (the `merge_events.kind` CHECK) and a feedback label
    "criterion": lambda: {*LLM_CRITERIA, *DET_CRITERIA},
    "basis": basis_values,
    "posted_precision": lambda: literal_values(Precision),
    "salary_period": lambda: literal_values(Period),
    "result_status": lambda: literal_values(ScoreOut.model_fields["result_status"].annotation),
    "critic_outcome": lambda: literal_values(DetailScoreOut.model_fields["critic_outcome"].annotation),
    "critic_trigger": lambda: literal_values(Trigger),
    "critic_verdict": lambda: literal_values(CriticOut.model_fields["verdict"].annotation),
    "round_outcome": lambda: literal_values(DerivedOutcome),
    "critic_issue": lambda: {
        *validator.CRITERION_ASSESSMENTS,
        FACT_REJECTED,
        FACT_UNREVIEWED,
        FACT_UNCERTAIN,
    },
    "cap": lambda: literal_values(KnockoutCode) | {aggregate.INDUSTRY_CODE},
    "cap_kind": lambda: literal_values(aggregate.CapKind),
    "industry_match": lambda: literal_values(aggregate.IndustryMatch),
    "profile_section": lambda: set(validator.PROFILE_SECTIONS),
    "merge_kind": lambda: table_check_values(COLLECTION_MIGRATION, "merge_events", "kind"),
    "feedback_label": lambda: literal_values(FeedbackLabel),
    # a duplicate group's members (plan M4 Task 25): a job's completeness (the `jobs.completeness` CHECK); the
    # representative's `election_reason` has no closed set to derive from (a free `text` column whose writers
    # pass the constants of `dedup.groups`), so the UI shows it verbatim
    "completeness": lambda: table_check_values(COLLECTION_MIGRATION, "jobs", "completeness"),
    # the CV and profile section (plan M4 Task 27): why a draft failed, the strip report's contact categories,
    # and the closed sets of the profile form a namespace above does not cover (`seniority` covers the
    # profile's, but not a role's `unspecified`; `degree_level` an education's level)
    "draft_failure": draft_failure_codes,
    "contact_category": lambda: literal_values(Category),
    "role_seniority": lambda: literal_values(profile_schema.Role.model_fields["seniority"].annotation),
    "skill_category": lambda: literal_values(profile_schema.Skill.model_fields["category"].annotation),
    "skill_level": lambda: literal_values(profile_schema.Skill.model_fields["level"].annotation),
    "cefr": lambda: literal_values(profile_schema.Language.model_fields["cefr"].annotation),
    "language_code": lambda: literal_values(profile_schema.Language.model_fields["code"].annotation),
    # the settings form (plan M4 Task 28, resolution h): the dry-run's effect chips, the source of `ĉ` (the
    # dry-run's and the spend read's), the severity of a knockout or a working language, and the eval gate a
    # gated change would check
    "effect_class": lambda: literal_values(EffectClass),
    "c_hat_source": lambda: (
        literal_values(CHatSource) | literal_values(spend.SpendOut.model_fields["c_hat_source"].annotation)
    ),
    "severity": lambda: literal_values(settings_model.Severity),
    "eval_gate": lambda: literal_values(GateState),
    # Queries & sources and the watchlist (plan M4 Task 29, resolutions c to e): a query's origin, why a query
    # proposal failed, a watchlist company's validation, why a careers URL names no board, and Lever's region
    "query_origin": lambda: literal_values(authoring.Origin),
    "proposal_failure": proposal_failure_codes,
    "watchlist_validation": lambda: literal_values(watchlist_listing.ValidationStatus),
    "watchlist_unsupported": lambda: literal_values(get_type_hints(watchlist_patterns.Unsupported)["reason"]),
    "lever_region": lambda: literal_values(watchlist_patterns.LeverRegion),
    # Scoring & models (plan M4 Task 30, resolutions c, e and f): the graph nodes of the model and execution
    # grids, the preset labels, a golden run's status, and the transport of a prompt-schema version's schema
    # dry-run; the gate states are `eval_gate`'s
    "llm_node": lambda: literal_values(settings_model.Node),
    "model_preset": lambda: literal_values(settings_model.PolicyLabel),
    "eval_run_status": lambda: literal_values(eval_runs.EvalStatus),
    "schema_transport": lambda: literal_values(schema_dry_run.Transport),
    # System, Display & notifications (plan M4 Task 31, resolutions c to g): the sections of Needs attention
    # in `AttentionOut`'s order; why an identity conflict holds when its evidence names no key namespace (the
    # namespaces it names are shown verbatim); a quarantined batch's state; a run's trigger (the
    # `runs.trigger` CHECK); a score cycle's trigger and status; a golden case's source; an alert's severity
    # (also the notification threshold's and a broken source's); the health panel's readiness probes, its
    # run-age state, the worker's queues and their jobs' statuses (procrastinate's); and the UI languages
    "attention_section": lambda: set(attention.AttentionOut.model_fields),
    "conflict_reason": lambda: {attention.NO_NAMESPACE_REASON},
    "batch_state": lambda: literal_values(attention.QuarantinedBatchOut.model_fields["state"].annotation),
    "run_trigger": lambda: table_check_values(BASELINE_MIGRATION, "runs", "trigger"),
    "cycle_trigger": lambda: literal_values(CycleTrigger),
    "cycle_status": lambda: literal_values(CycleStatus),
    "golden_source": lambda: literal_values(CaseSource),
    "alert_severity": lambda: literal_values(alert_catalogue.Severity),
    "readiness": lambda: literal_values(health.ReadinessCode),
    "run_age_state": lambda: literal_values(health.RunAgeState),
    "queue": lambda: set(QUEUES),
    "queue_status": lambda: {status.value for status in QueueJobStatus},
    "ui_language": lambda: literal_values(settings_model.Display.model_fields["ui_language"].annotation),
}

# The profile form's closed sets that an existing namespace covers (plan M4 Task 27, resolution g): each
# literal is a subset of the namespace it is shown through.
PROFILE_FORM_REUSED: Final[dict[str, Callable[[], set[str]]]] = {
    "seniority": lambda: literal_values(profile_schema.ProfileOut.model_fields["seniority"].annotation),
    "degree_level": lambda: literal_values(profile_schema.Education.model_fields["level"].annotation),
    "work_authorization": lambda: literal_values(
        profile_schema.UserEntered.model_fields["work_authorization_de"].annotation
    ),
}


# The source switch of every watchlist board (`sources.enabled.ats`): no source of its own, so it has a
# label of its own (`settings_queries.source_ats`), not a `source` code.
ATS_SWITCH: Final = "ats"

# The settings form's closed sets that an existing namespace covers (plan M4 Task 28): each literal of the
# settings model is a subset of the namespace it is shown through. The knockout severities are keyed by the
# knockout codes; the work modes are the Jobs list filter's own labels (`filters.work_mode.*`), held to the
# model by the frontend's types.
SETTINGS_FORM_REUSED: Final[dict[str, Callable[[], set[str]]]] = {
    "employment": lambda: literal_values(settings_model.EmploymentType),
    "seniority": lambda: literal_values(settings_model.SeniorityLevel),
    # also a query's language (Queries & sources, plan M4 Task 29)
    "language_code": lambda: (
        literal_values(settings_model.LanguageCode)
        | literal_values(settings_model.AdLanguage)
        | literal_values(authoring.Lang)
    ),
    "knockout": lambda: set(settings_model.KnockoutSeverity.model_fields),
    # the source switches but `ats` (all the watchlist boards, a label of its own: `ATS_SWITCH`), and the ATS
    # of a watchlist board (plan M4 Task 29, resolutions b and d)
    "source": lambda: (
        (set(settings_model.SourceSwitches.model_fields) - {ATS_SWITCH})
        | literal_values(watchlist_patterns.AtsType)
    ),
    # the critic's light-effort triggers (Scoring & models, plan M4 Task 30)
    "critic_trigger": lambda: literal_values(settings_model.LightTrigger),
}


# The closed sets of System and Display & notifications that an existing namespace covers (plan M4 Task 31,
# resolution g): each is a subset of the namespace it is shown through.
SYSTEM_REUSED: Final[dict[str, Callable[[], set[str]]]] = {
    # the notification threshold, and a broken source's severity
    "alert_severity": lambda: (
        literal_values(settings_model.Notifications.model_fields["push_min_severity"].annotation)
        | literal_values(attention.BrokenSeverity)
    ),
    # the reason of the score lane's acknowledgeable pause (the health panel's is `PauseReason` itself)
    "pause": lambda: literal_values(attention.PausedReason),
    "group_unscorable": lambda: literal_values(attention.UnscorableReason),
    "instance_health": lambda: literal_values(attention.BrokenHealth),
}


# The namespaces the UI lists in an order of their own, with that order from the backend.
ORDERS: Final[dict[str, Callable[[], list[str]]]] = {
    "seniority": lambda: list(SENIORITY_LADDER),
    # Needs attention's blocks, in `AttentionOut`'s order (plan M4 Task 31, review Minor 3)
    "attention_section": lambda: list(attention.AttentionOut.model_fields),
}
ORDER_KEY: Final = "order"
# Each model's supported efforts in `Effort`'s order (module docstring): `order.effort.<model>`.
EFFORT_ORDER: Final = "effort"
# The target states the retry route takes back (`scoring.targets.RETRYABLE_STATES`, in its order), a subset
# of `target_state`: Needs attention offers Retry on an unscorable group's member in one of them (plan M4 Task
# 31, review Minor 1). No namespace: the states are shown through `target_state`.
RETRYABLE_ORDER: Final = "retryable_target_state"


def model_efforts() -> dict[str, list[str]]:
    """Each model of §10.3 with the efforts `llm.pricing` lists it as supporting, in `Effort`'s order."""
    return {
        model: [effort for effort in get_args(pricing.Effort) if effort in pricing.SUPPORTED_EFFORTS[model]]
        for model in pricing.MODELS
    }


@pytest.fixture(scope="module")
def catalogue() -> dict[str, Any]:
    loaded = json.loads(CODES.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_catalogue_has_exactly_the_namespaces(catalogue: dict[str, Any]) -> None:
    assert sorted(set(catalogue) - {ORDER_KEY}) == sorted(NAMESPACES)


def test_each_order_is_its_backend_order_of_its_namespace(catalogue: dict[str, Any]) -> None:
    orders = catalogue.get(ORDER_KEY)
    keys = sorted([*ORDERS, EFFORT_ORDER, RETRYABLE_ORDER])
    assert isinstance(orders, dict) and sorted(orders) == keys, orders
    for namespace, expected in ORDERS.items():
        assert orders[namespace] == expected(), f"codes.json[order][{namespace}]"
        assert sorted(orders[namespace]) == catalogue[namespace], f"order[{namespace}] is not its namespace"


def test_the_retryable_states_are_the_retry_route_s(catalogue: dict[str, Any]) -> None:
    retryable = catalogue[ORDER_KEY].get(RETRYABLE_ORDER)
    assert retryable == list(targets.RETRYABLE_STATES), f"codes.json[order][{RETRYABLE_ORDER}]"
    assert set(retryable) <= set(catalogue["target_state"]), "a retryable state that is no target state"


def test_each_model_s_efforts_are_its_supported_efforts_in_order(catalogue: dict[str, Any]) -> None:
    efforts = catalogue[ORDER_KEY].get(EFFORT_ORDER)
    assert efforts == model_efforts(), f"codes.json[order][{EFFORT_ORDER}]"
    assert all(efforts[model] for model in pricing.MODELS), "a model without an effort"


@pytest.mark.parametrize("namespace", sorted(NAMESPACES))
def test_each_namespace_is_a_sorted_list_of_distinct_codes(catalogue: dict[str, Any], namespace: str) -> None:
    codes = catalogue.get(namespace)
    assert isinstance(codes, list) and all(isinstance(code, str) for code in codes), codes
    assert codes == sorted(set(codes)), f"codes.json[{namespace}] is not sorted, or repeats a code"


@pytest.mark.parametrize("namespace", sorted(NAMESPACES))
def test_each_namespace_equals_its_backend_source(catalogue: dict[str, Any], namespace: str) -> None:
    expected = NAMESPACES[namespace]()
    listed = set(catalogue.get(namespace, []))
    missing, extra = sorted(expected - listed), sorted(listed - expected)
    assert not missing and not extra, f"codes.json[{namespace}]: missing {missing}, extra {extra}"


@pytest.mark.parametrize("namespace", sorted(PROFILE_FORM_REUSED))
def test_the_profile_form_s_reused_namespaces_cover_their_literals(
    catalogue: dict[str, Any], namespace: str
) -> None:
    missing = sorted(PROFILE_FORM_REUSED[namespace]() - set(catalogue[namespace]))
    assert not missing, f"codes.json[{namespace}] lacks {missing}"


@pytest.mark.parametrize("namespace", sorted(SETTINGS_FORM_REUSED))
def test_the_settings_form_s_reused_namespaces_cover_their_literals(
    catalogue: dict[str, Any], namespace: str
) -> None:
    missing = sorted(SETTINGS_FORM_REUSED[namespace]() - set(catalogue[namespace]))
    assert not missing, f"codes.json[{namespace}] lacks {missing}"


@pytest.mark.parametrize("namespace", sorted(SYSTEM_REUSED))
def test_the_system_section_s_reused_namespaces_cover_their_literals(
    catalogue: dict[str, Any], namespace: str
) -> None:
    missing = sorted(SYSTEM_REUSED[namespace]() - set(catalogue[namespace]))
    assert not missing, f"codes.json[{namespace}] lacks {missing}"


def test_the_attention_sections_have_no_failed_purges_section() -> None:
    """Needs attention shows one block per section of `AttentionOut` (their order is
    `order.attention_section`, held by `test_each_order_is_its_backend_order_of_its_namespace`), and none for
    failed purges (U1)."""
    sections = list(attention.AttentionOut.model_fields)
    assert not [name for name in sections if "purge" in name], sections


def test_a_role_s_seniority_is_the_profile_s_and_unspecified(catalogue: dict[str, Any]) -> None:
    """Why `role_seniority` is a namespace of its own: `seniority` lacks `unspecified`."""
    assert set(catalogue["role_seniority"]) == set(catalogue["seniority"]) | {"unspecified"}


def test_the_runner_s_codes_are_found() -> None:
    """The walk finds the runner's three literal codes: a change in how they are passed cannot drop them."""
    assert operation_runner_codes() == {"no_handler", "bug", "already_executing"}


def test_the_runner_s_failure_codes_are_found() -> None:
    """The one code the runner itself fails an operation with: a change in how it is passed cannot drop it."""
    assert operation_runner_failure_codes() == {"no_handler"}


def test_the_propose_node_s_own_failure_codes_are_found() -> None:
    """The node's one `Fail` code of its own, besides `llm_outcome`'s: a change in how it is passed cannot
    drop it, and the brief's three codes are among the proposal's failures."""
    assert call_args(propose_node.__name__, "Fail") == {propose_node.PROFILE_UNAVAILABLE}
    assert {"budget_exhausted", "invalid_output", "refused"} <= proposal_failure_codes()


def test_the_alert_walk_sees_the_gateway_s_classified_alerts() -> None:
    """`gateway.raise_classified_alert` passes its codes to `raise_alert` as module constants (G11, D6)."""
    assert {gateway.MODEL_MISMATCH_ALERT, gateway.UNEXPECTED_STOP_ALERT} <= alert_codes()


@pytest.mark.parametrize("module", ["settings/validate.py", "settings/dryrun.py", "quota/dryrun.py"])
def test_the_warning_walk_reads_each_module_that_warns(module: str) -> None:
    """`settings/validate.py` holds only some of the warnings; the rest are in the two dry-run modules."""
    assert warning_codes((PACKAGE / module,))


# Two modules beside a wrapper's own, which call it through the module and by its imported name.
WRAPPER: Final = """
async def opened(conn, *, code, message):
    await open_alert(conn, code=code, severity="warn", subject="s", message=message)
"""


def synthetic(modules: Mapping[str, str]) -> list[tuple[str, ast.Module]]:
    return [(name, ast.parse(textwrap.dedent(source))) for name, source in modules.items()]


def test_a_wrapper_s_callers_count_in_any_module_by_name_or_through_the_module() -> None:
    walk = synthetic(
        {
            "pkg.alerts": WRAPPER,
            "pkg.jobs": """
                from pkg import alerts
                async def check(conn):
                    await alerts.opened(conn, code="pkg.through_the_module", message="m")
            """,
            "pkg.runs": """
                from pkg.alerts import opened
                async def check(conn):
                    await opened(conn, code="pkg.by_name", message="m")
            """,
        }
    )
    assert alert_codes(walk, dynamic={}) == {"pkg.through_the_module", "pkg.by_name"}


def test_a_wrapper_s_caller_that_passes_a_computed_code_fails_naming_it() -> None:
    walk = synthetic(
        {
            "pkg.alerts": WRAPPER,
            "pkg.jobs": """
                from pkg import alerts
                async def check(conn, which):
                    await alerts.opened(conn, code=which, message="m")
            """,
        }
    )
    with pytest.raises(AssertionError, match=r"pkg\.jobs:\d+: opened\(code=which\) is computed"):
        alert_codes(walk, dynamic={})


def test_the_walk_finds_the_forwarding_wrapper_of_the_score_lane() -> None:
    """Today's one wrapper, so a change in how wrappers are found cannot drop its five callers silently."""
    forwarding = {(module, name) for module, tree in sources() for name in wrappers(tree)}
    assert forwarding == {("jobhunter.scoring.reconcilers", "_opened")}
