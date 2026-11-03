"""`frontend/src/i18n/codes.json` (plan M4 Task 20): the server code catalogues the SPA translates (D39).

Each namespace equals its backend source, derived from the code and never typed out here: a Literal's
arguments (`typing.get_args`), a mapping's keys, or an AST walk of the calls that pass a code. A failure names
the missing and the extra codes. The catalogue's own form is pinned too: exactly these namespaces, each a
sorted list of distinct strings (the frontend's `i18n.test.ts` checks that both languages have a key for each
code).

The alert codes are the `code` of every `raise_alert` and `open_alert` call in `backend/src`, with a module
constant resolved by importing its module, and the codes of a module's wrapper that forwards its own `code`
parameter to one of them (`scoring.reconcilers._opened`). A call whose code is computed (a loop variable, a
mapping's value) is one of `DYNAMIC_SITES`, which derives that site's codes from the object the code comes
from; a computed call not listed there fails the test, as does a listed site the walk no longer finds.
"""

import ast
import importlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import UnionType
from typing import Any, Final, Literal, TypeAliasType, Union, get_args, get_origin

import pytest
from jobhunter.api.routes import profile_review
from jobhunter.api.routes import settings as settings_routes
from jobhunter.db import ranking
from jobhunter.errors import ErrorCode
from jobhunter.graphs.collect.nodes.finalize import PartialCause
from jobhunter.llm import gateway
from jobhunter.observability.health import RunStatus
from jobhunter.observability.lists import AlertStatus
from jobhunter.observability.status import BannerCode
from jobhunter.profile.errors import RejectReason
from jobhunter.profile.review import Repair
from jobhunter.quota import windows
from jobhunter.quota.windows import WindowState
from jobhunter.scoring.cycles import PauseReason
from jobhunter.sources import breaker
from jobhunter.worker import runs

REPO: Final = Path(__file__).resolve().parents[2]
SRC: Final = REPO / "backend/src"
PACKAGE: Final = SRC / "jobhunter"
CODES: Final = REPO / "frontend/src/i18n/codes.json"
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


def alert_codes() -> set[str]:
    codes: set[str] = set()
    computed: set[tuple[str, str]] = set()
    for module, tree in sources():
        forwarding = wrappers(tree)
        for call, function in calls_by_function(tree):
            name = called_name(call)
            if name not in ALERT_FUNCTIONS and not (isinstance(call.func, ast.Name) and name in forwarding):
                continue
            expr = keyword(call, "code")
            assert expr is not None, f"{module}:{call.lineno}: {name}() without code="
            if function is not None and function.name in forwarding and name in ALERT_FUNCTIONS:
                continue  # the wrapper's own forwarding call: its callers pass the codes
            code = resolve(module, expr, function)
            if code is not None:
                codes.add(code)
                continue
            site = (module, function.name if function is not None else "<module>")
            assert site in DYNAMIC_SITES, (
                f"{module}:{call.lineno}: {name}(code={ast.unparse(expr)}) is computed; add the site to "
                "DYNAMIC_SITES with the source of its codes"
            )
            computed.add(site)
            codes |= DYNAMIC_SITES[site]()
    stale = set(DYNAMIC_SITES) - computed
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
    "window_state": lambda: literal_values(WindowState),
    "instance_health": lambda: literal_values(breaker.Health),
    "alert_status": lambda: literal_values(AlertStatus),
}


@pytest.fixture(scope="module")
def catalogue() -> dict[str, Any]:
    loaded = json.loads(CODES.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_catalogue_has_exactly_the_namespaces(catalogue: dict[str, Any]) -> None:
    assert sorted(catalogue) == sorted(NAMESPACES)


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


def test_the_alert_walk_sees_the_gateway_s_classified_alerts() -> None:
    """`gateway.raise_classified_alert` passes its codes to `raise_alert` as module constants (G11, D6)."""
    assert {gateway.MODEL_MISMATCH_ALERT, gateway.UNEXPECTED_STOP_ALERT} <= alert_codes()


@pytest.mark.parametrize("module", ["settings/validate.py", "settings/dryrun.py", "quota/dryrun.py"])
def test_the_warning_walk_reads_each_module_that_warns(module: str) -> None:
    """`settings/validate.py` holds only some of the warnings; the rest are in the two dry-run modules."""
    assert warning_codes((PACKAGE / module,))
