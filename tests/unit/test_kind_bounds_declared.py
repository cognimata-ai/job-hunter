"""Each purge kind's declared `max_ids` keys cover every bound its procedure reads (final fix B9; fix A's
concern 4): a static check over the kind modules' syntax trees, so a read added without its declaration fails
here, not as a bare `KeyError` in a replay of a manifest that lacks the key.

`KindProcedure.max_ids_keys` is what a replay requires of every manifest before it starts
(`privacy.replay.required_bounds`). The procedure reads a manifest's bounds in its `execute`, its
`postconditions` and its other methods, and in the module's functions they reach; `compute` reads the bounds
it has just taken (`max_identity_ids`, every identity table), never a manifest's, so what only `compute`
reaches is left out. A bound read is a subscript or a `.get` of a manifest's bounds: `<content or
scope>["max_ids"]` or `<content or scope>.get("max_ids", …)`, a `:=` whose value is one, a name bound to one
in the same function (by `=`, an annotated `=` or `:=`, an alias of an alias included: final fix C4), a
parameter named `max_ids` or `bounds` (the modules' names for them), or any parameter a call in the module
gives one. A key of the content (`content`, `scope`, a name bound to one, or a parameter a call in the module
gives it) that is no string literal fails the check, since it may be `"max_ids"` (final fixes D5 and E3); so
does any use of the content but a subscript, a `.get`, an argument of a function of the module that binds to a
named parameter of it, by position or by keyword (the final re-review's R4-M2), an alias and an identity test
(another method of it, its iteration, a function outside the module given it whole: final fix E3; an argument
that lands in a `*args` or `**kwargs` parameter, or follows an unpacking, where no name holds it: R4-M1). Any
other use of a tracked bound (iterated, its `.items()`, kept on an attribute, given to a function of the
module in no named parameter) fails the check too, as an unhandled use. Its key is a string, a module
constant, a loop variable over a literal or a module constant (a tuple's element too), a local name bound to
such a literal, or a parameter, resolved through every call of its function in the module (a `*args` parameter
through every argument from its position on). A read whose key cannot be resolved fails the check as well, by
function and line, and so does a manifest's bounds passed whole to a function outside the module, unless
`PASSED_OUT` names it: a pure function, whose reads are found by calling it.

A kind declares the keys it requires (`max_ids_keys`) and those a later release added
(`max_ids_keys_added`, final fix C3): a table created empty after a manifest was written has no row within
that manifest's bound, so an added key's absence reads as 0, and the check requires every read of one to be
`.get(key, 0)`.

The check is shown to fail on reads planted in a kind's syntax tree: a key it does not declare, read in
`execute`, through `.get`, or in a helper its `postconditions` call; a key no rule resolves; the bounds passed
whole to another module; an added key read by subscript or with another default; a read through an annotated
alias or an alias of an alias; a use of the bounds the check does not follow (iterated, kept on an attribute);
the bounds taken by `.get("max_ids")`, with or without a default, a computed key of the content, and a
subscripted `:=` (final fix D5); a computed key on an alias of the content, another method of the content
(`__getitem__`), and the content iterated (final fix E3); the content or the bounds given to a `*args` or
`**kwargs` parameter or after an unpacking (R4-M1). A keyword argument is followed into its parameter, a
keyword-only one too (R4-M2). A read planted in `compute` alone is left out, and an added key read as
`.get(key, 0)`, or a declared bound read through a keyword call, passes, as the check says.
"""

import ast
import inspect
from collections.abc import Callable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Final, cast

import pytest
from jobhunter.privacy import manifests
from jobhunter.privacy.kinds import delete_everything, golden_case, job_tombstone, profile_purge, source_purge
from jobhunter.privacy.purge import KINDS, register_kind

MODULES: Final = (golden_case, job_tombstone, profile_purge, source_purge, delete_everything)
BOUNDS_NAMES: Final = frozenset({"max_ids", "bounds"})
# The modules' names for a manifest's content, whose `"max_ids"` key holds its bounds (final fix D5).
CONTENT_NAMES: Final = frozenset({"content", "scope"})
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass
class Function:
    """A function of the module, or a method (`Class.method`), with its node."""

    name: str
    node: FunctionNode
    owner: str | None = None


@dataclass
class Reads:
    """The bound keys a procedure reads, and the reads whose key is unresolved (`function:line`), a manifest's
    bounds passed whole to a function outside the module among them."""

    keys: set[str] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)
    # each key's read forms: `subscript`, `get_or_0` (`.get(key, 0)`) or `get` (another `.get`)
    forms: dict[str, set[str]] = field(default_factory=dict)


def functions(tree: ast.Module) -> dict[str, Function]:
    """Every function of the module and every method of its classes, by qualified name."""
    found: dict[str, Function] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found[node.name] = Function(node.name, node)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    found[f"{node.name}.{item.name}"] = Function(item.name, item, node.name)
    return found


def callees(function: Function, known: Mapping[str, Function]) -> set[str]:
    """The functions `function` names: a module function by its name, a method of its own class by `self.`."""
    named: set[str] = set()
    for node in ast.walk(function.node):
        if isinstance(node, ast.Name) and node.id in known and known[node.id].owner is None:
            named.add(node.id)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and f"{function.owner}.{node.attr}" in known
        ):
            named.add(f"{function.owner}.{node.attr}")
    return named - {qualified(function)}


def qualified(function: Function) -> str:
    return function.name if function.owner is None else f"{function.owner}.{function.name}"


def replay_reach(known: Mapping[str, Function], procedure: str) -> set[str]:
    """What the procedure's methods but `compute`, and every function no other function names, reach (module
    docstring)."""
    edges = {name: callees(function, known) for name, function in known.items()}
    named = {callee for name, called in edges.items() for callee in called}
    roots = {name for name in known if name not in named and name != f"{procedure}.compute"}
    roots |= {name for name in known if name.startswith(f"{procedure}.") and name != f"{procedure}.compute"}
    reached, todo = set(), list(roots)
    while todo:
        name = todo.pop()
        if name not in reached:
            reached.add(name)
            todo.extend(edges[name])
    return reached


class Resolver:
    """Resolves bound keys and the iterables a key loops over (module docstring)."""

    def __init__(self, namespace: Mapping[str, Any], known: Mapping[str, Function]) -> None:
        self.namespace = namespace
        self.known = known

    def key(self, expr: ast.expr, function: Function, depth: int = 0) -> set[str] | None:
        if depth > 8:
            return None
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if not isinstance(expr, ast.Name):
            return None
        if loops := self.loops_of(expr.id, function):
            found: set[str] = set()
            for iterable, index in loops:  # a name several loops bind takes each one's items
                part = self.items(iterable, function, index, depth + 1)
                if part is None:
                    return None
                found |= part
            return found
        if (local := self.local_of(expr.id, function)) is not None:
            return self.key(local, function, depth + 1)
        if expr.id in parameters(function.node):
            return self.argument(expr.id, function, star=False, depth=depth + 1)
        value = self.namespace.get(expr.id)
        return {value} if isinstance(value, str) else None

    def items(self, expr: ast.expr, function: Function, index: int | None, depth: int) -> set[str] | None:
        """The keys `expr` iterates over, each item's element `index` when the loop unpacks a tuple."""
        if depth > 8:
            return None
        if isinstance(expr, ast.Tuple | ast.List | ast.Set):
            found: set[str] = set()
            for element in expr.elts:
                if isinstance(element, ast.Starred):
                    part = self.items(element.value, function, index, depth + 1)
                elif index is None:
                    part = self.key(element, function, depth + 1)
                elif isinstance(element, ast.Tuple) and index < len(element.elts):
                    part = self.key(element.elts[index], function, depth + 1)
                else:
                    part = None
                if part is None:
                    return None
                found |= part
            return found
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id in ("sorted", "list")
        ):
            return self.items(expr.args[0], function, index, depth + 1) if len(expr.args) == 1 else None
        if not isinstance(expr, ast.Name):
            return None
        if (local := self.local_of(expr.id, function)) is not None:
            return self.items(local, function, index, depth + 1)
        if expr.id in parameters(function.node):
            if index is None and expr.id == star_of(function.node):
                return self.argument(expr.id, function, star=True, depth=depth + 1)
            return None
        return constant_items(self.namespace.get(expr.id), index)

    def argument(self, name: str, function: Function, *, star: bool, depth: int) -> set[str] | None:
        """The keys every call of `function` in the module passes as its parameter `name`."""
        positional = [arg.arg for arg in (*function.node.args.posonlyargs, *function.node.args.args)]
        if function.owner is not None:
            positional = positional[1:]  # self
        found: set[str] = set()
        calls = 0
        for caller in self.known.values():
            for call in ast.walk(caller.node):
                if not (isinstance(call, ast.Call) and self.calls(call, function, caller)):
                    continue
                calls += 1
                if star:
                    passed = call.args[len(positional) :]
                else:
                    keyword = [k.value for k in call.keywords if k.arg == name]
                    at = positional.index(name) if name in positional else None
                    passed = keyword or ([call.args[at]] if at is not None and at < len(call.args) else [])
                    if len(passed) != 1:
                        return None
                for arg in passed:
                    part = (
                        self.items(arg.value, caller, None, depth + 1)
                        if isinstance(arg, ast.Starred)
                        else self.key(arg, caller, depth + 1)
                    )
                    if part is None:
                        return None
                    found |= part
        return found if calls else None

    def calls(self, call: ast.Call, function: Function, caller: Function) -> bool:
        if function.owner is None:
            return isinstance(call.func, ast.Name) and call.func.id == function.name
        return (
            caller.owner == function.owner
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
            and call.func.attr == function.name
        )

    @staticmethod
    def loops_of(name: str, function: Function) -> list[tuple[ast.expr, int | None]]:
        """Each iterable `name` loops over in `function`, with its element's index when unpacked."""
        found: list[tuple[ast.expr, int | None]] = []
        for node in ast.walk(function.node):
            if not isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
                continue
            if isinstance(node.target, ast.Name) and node.target.id == name:
                found.append((node.iter, None))
            elif isinstance(node.target, ast.Tuple):
                found.extend(
                    (node.iter, at)
                    for at, element in enumerate(node.target.elts)
                    if isinstance(element, ast.Name) and element.id == name
                )
        return found

    @staticmethod
    def local_of(name: str, function: Function) -> ast.expr | None:
        """The expression `name` is assigned once in `function`, when it is."""
        values = [
            node.value
            for node in ast.walk(function.node)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        ]
        return values[0] if len(values) == 1 else None


def parameters(node: FunctionNode) -> set[str]:
    args = node.args
    names = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    names |= {arg.arg for arg in (args.vararg, args.kwarg) if arg is not None}
    return names


def star_of(node: FunctionNode) -> str | None:
    return None if node.args.vararg is None else node.args.vararg.arg


def constant_items(value: Any, index: int | None) -> set[str] | None:
    if not isinstance(value, tuple | list | set | frozenset):
        return None
    items = [item if index is None else (item[index] if isinstance(item, tuple) else None) for item in value]
    keys = {item for item in items if isinstance(item, str)}
    return keys if len(keys) == len(set(items)) else None


def bindings(function: Function) -> Iterator[tuple[ast.expr, ast.expr]]:
    """Every (target, value) `function` binds: `=` (a tuple's elements pairwise), an annotated `=`, and
    `:=`."""
    for node in ast.walk(function.node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    yield from zip(target.elts, node.value.elts, strict=False)
                else:
                    yield target, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            yield node.target, node.value
        elif isinstance(node, ast.NamedExpr):
            yield node.target, node.value


def bounds_names(function: Function, given: AbstractSet[str] = frozenset()) -> set[str]:
    """The names that hold a manifest's bounds in `function`: its parameters named so, those a caller in the
    module gives a manifest's bounds (`given`), and every name it binds to one, by `=` (a tuple's element
    too), an annotated `=` or `:=`, from a `["max_ids"]` subscript or another such name, to a fixed point
    (final fix C4: an alias of an alias)."""
    names = (parameters(function.node) & BOUNDS_NAMES) | set(given)
    changed = True
    while changed:
        changed = False
        for target, value in bindings(function):
            if isinstance(target, ast.Name) and target.id not in names and holding(value, names):
                names.add(target.id)
                changed = True
    return names


def unhandled_uses(function: Function, holders: set[str], known: Mapping[str, Function]) -> Iterator[int]:
    """The line of every use of a manifest's bounds in `function` that the check does not follow (final fix
    C4): any but a subscript read, a `.get(...)` read, an argument of a call (whose callee `escapes_in` and
    `given_bounds` judge; for a function of the module, one that binds to a named parameter of it, by
    position or by keyword: `binds_to_a_parameter`) and the value bound to a name (an alias). A use is of a
    name that holds them, of a `["max_ids"]` subscript or a `.get("max_ids", …)`, or of a `:=` whose value
    holds them (final fix D5)."""
    parents = {child: node for node in ast.walk(function.node) for child in ast.iter_child_nodes(node)}
    aliased = {id(value) for target, value in bindings(function) if isinstance(target, ast.Name)}
    for node in ast.walk(function.node):
        if isinstance(node, ast.Name | ast.Subscript) and not isinstance(node.ctx, ast.Load):
            continue
        if not isinstance(node, ast.Name | ast.Subscript | ast.Call | ast.NamedExpr):
            continue
        if not holding(node, holders):
            continue
        parent = parents.get(node)
        read = isinstance(parent, ast.Subscript) and parent.value is node and isinstance(parent.ctx, ast.Load)
        get = (
            isinstance(parent, ast.Attribute)
            and parent.value is node
            and parent.attr == "get"
            and isinstance(parents.get(parent), ast.Call)
            and cast(ast.Call, parents[parent]).func is parent
        )
        call = argument_of(node, parents)
        callee = None if call is None else callee_of(call, function, known)
        argument = call is not None and (callee is None or binds_to_a_parameter(call, node, known[callee]))
        if not (read or get or argument or id(node) in aliased):
            yield node.lineno


def is_max_ids(expr: ast.expr) -> bool:
    """`<anything>["max_ids"]`, or `<anything>.get("max_ids", …)` (final fix D5)."""
    if isinstance(expr, ast.Subscript):
        return isinstance(expr.slice, ast.Constant) and expr.slice.value == "max_ids"
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "get"
        and bool(expr.args)
        and isinstance(expr.args[0], ast.Constant)
        and expr.args[0].value == "max_ids"
    )


def is_content(expr: ast.expr, holders: AbstractSet[str]) -> bool:
    """Whether `expr` is a manifest's content: a name that holds it, an attribute named `content` or `scope`,
    a `:=` whose value is one, or a conditional expression either branch of which is one."""
    if isinstance(expr, ast.NamedExpr):
        return is_content(expr.value, holders)
    if isinstance(expr, ast.IfExp):
        return is_content(expr.body, holders) or is_content(expr.orelse, holders)
    return (isinstance(expr, ast.Name) and expr.id in holders) or (
        isinstance(expr, ast.Attribute) and expr.attr in CONTENT_NAMES
    )


def content_names(function: Function, given: AbstractSet[str] = frozenset()) -> set[str]:
    """The names that hold a manifest's content in `function`: `content` and `scope` (the modules' names for
    it), those a caller in the module gives it (`given`), and every name it binds to one, by `=`, an annotated
    `=` or `:=`, to a fixed point (final fix E3: an alias)."""
    names = set(CONTENT_NAMES) | set(given)
    changed = True
    while changed:
        changed = False
        for target, value in bindings(function):
            if isinstance(target, ast.Name) and target.id not in names and is_content(value, names):
                names.add(target.id)
                changed = True
    return names


def given_content(known: Mapping[str, Function]) -> dict[str, set[str]]:
    """Each function's parameters that a call in the module gives a manifest's content, whatever their names,
    to a fixpoint (as `given_bounds` for the bounds)."""
    given: dict[str, set[str]] = {name: set() for name in known}
    changed = True
    while changed:
        changed = False
        for name, caller in known.items():
            holders = content_names(caller, given[name])
            for call in (node for node in ast.walk(caller.node) if isinstance(node, ast.Call)):
                callee = callee_of(call, caller, known)
                if callee is None:
                    continue
                target = known[callee]
                positional = [arg.arg for arg in (*target.node.args.posonlyargs, *target.node.args.args)]
                if target.owner is not None:
                    positional = positional[1:]  # self
                found = {
                    positional[at]
                    for at, arg in enumerate(call.args[: len(positional)])
                    if is_content(arg, holders)
                }
                found |= {
                    kw.arg for kw in call.keywords if kw.arg is not None and is_content(kw.value, holders)
                }
                if not found <= given[callee]:
                    given[callee] |= found
                    changed = True
    return given


def argument_of(node: ast.expr, parents: Mapping[ast.AST, ast.AST]) -> ast.Call | None:
    """The call `node` is an argument of, by position or as a keyword's value (`f(c=node)`: its parent is the
    `ast.keyword`, the final re-review's R4-M2); `None` for any other use."""
    parent = parents.get(node)
    if isinstance(parent, ast.keyword) and parent.value is node:
        parent = parents.get(parent)
        return parent if isinstance(parent, ast.Call) else None
    if isinstance(parent, ast.Call) and any(arg is node for arg in parent.args):
        return parent
    return None


def binds_to_a_parameter(call: ast.Call, node: ast.expr, callee: Function) -> bool:
    """Whether the argument `node` of `call` binds to a named parameter of `callee`, the one `given_content`
    and `given_bounds` then hold it in: a positional argument within the named positional parameters, with no
    unpacking (`*xs`) before it, or a keyword naming a parameter that takes one. An argument that lands in
    `*args` or `**kwargs`, a `**mapping` and one after an unpacking bind to no name the check follows (the
    final re-review's R4-M1)."""
    args = callee.node.args
    positional = [arg.arg for arg in (*args.posonlyargs, *args.args)]
    if callee.owner is not None:
        positional = positional[1:]  # self
    for at, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred):
            return False
        if arg is node:
            return at < len(positional)
    by_keyword = {arg.arg for arg in (*args.args, *args.kwonlyargs)}
    return any(keyword.value is node and keyword.arg in by_keyword for keyword in call.keywords)


def _literal(key: ast.expr | None) -> bool:
    return isinstance(key, ast.Constant) and isinstance(key.value, str)


def content_uses(
    function: Function, holders: AbstractSet[str], known: Mapping[str, Function]
) -> Iterator[tuple[int, str]]:
    """The line of every use of a manifest's content (`content_names`, an attribute named `content` or
    `scope`) that the check cannot follow, with what it is. Its bounds are `content["max_ids"]`, or a `.get`
    of that literal key, so a key that is no string literal, by subscript or `.get`, may be `"max_ids"`: a
    `computed key` (final fix D5, and on an alias, final fix E3). Any use but a subscript, a `.get`, an
    argument of a function of the module that binds to a named parameter of it, by position or by keyword
    (whose parameter then holds it, `given_content`; the final re-review's R4-M2), the value bound to a name
    (an alias) and an identity test (`is None`) is an `unhandled use of the content` (final fix E3): another
    method (`__getitem__`, `items`, `values`), an iteration, a function outside the module given it whole, an
    argument that lands in a `*args` or `**kwargs` parameter or follows an unpacking, where no name holds it
    (R4-M1). A branch of a conditional expression is judged by that expression's use."""
    parents = {child: node for node in ast.walk(function.node) for child in ast.iter_child_nodes(node)}
    aliased = {id(value) for target, value in bindings(function) if isinstance(target, ast.Name)}
    for found in ast.walk(function.node):
        if not isinstance(found, ast.Name | ast.Attribute | ast.NamedExpr):
            continue
        if isinstance(found, ast.Name | ast.Attribute) and not isinstance(found.ctx, ast.Load):
            continue
        if not is_content(found, holders):
            continue
        node: ast.expr = found
        while isinstance(parent := parents.get(node), ast.IfExp) and node is not parent.test:
            node = parent
        if isinstance(parent, ast.Compare) and all(isinstance(op, ast.Is | ast.IsNot) for op in parent.ops):
            continue
        if isinstance(parent, ast.Subscript) and parent.value is node:
            if isinstance(parent.ctx, ast.Load) and not _literal(parent.slice):
                yield node.lineno, "computed key"
            continue
        call = parents.get(parent) if isinstance(parent, ast.Attribute) else None
        if (
            isinstance(parent, ast.Attribute)
            and parent.attr == "get"
            and isinstance(call, ast.Call)
            and call.func is parent
        ):
            if not (call.args and _literal(call.args[0])):
                yield node.lineno, "computed key"
            continue
        call = argument_of(node, parents)
        callee = None if call is None else callee_of(call, function, known)
        if call is not None and callee is not None and binds_to_a_parameter(call, node, known[callee]):
            continue
        if id(node) in aliased:
            continue
        yield node.lineno, "unhandled use of the content"


class Recording(dict[str, int]):
    """A manifest's bounds that hold every key, each 0, and record the keys read."""

    def __init__(self) -> None:
        super().__init__()
        self.read: set[str] = set()

    def __contains__(self, key: object) -> bool:
        return True

    def __getitem__(self, key: str) -> int:
        self.read.add(key)
        return 0

    def get(self, key: str, default: object = None) -> int:
        return self[key]


def read_by(reader: Callable[[Mapping[str, int]], object]) -> set[str]:
    """The keys the pure function `reader` reads of the bounds it is given."""
    bounds = Recording()
    reader(bounds)
    return bounds.read


# The functions outside the kind modules that a procedure gives a manifest's bounds whole: each pure, so the
# keys it reads are found by calling it.
PASSED_OUT: Final[dict[str, Callable[[Mapping[str, int]], object]]] = {
    "thread_bound_sql": manifests.thread_bound_sql,
}


def callee_of(call: ast.Call, caller: Function, known: Mapping[str, Function]) -> str | None:
    """The module function, or the caller's own method, that `call` calls; `None` for another."""
    if isinstance(call.func, ast.Name) and call.func.id in known and known[call.func.id].owner is None:
        return call.func.id
    if (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        and f"{caller.owner}.{call.func.attr}" in known
    ):
        return f"{caller.owner}.{call.func.attr}"
    return None


def holding(expr: ast.expr, holders: set[str]) -> bool:
    """Whether `expr` is a manifest's bounds: a name that holds them, `is_max_ids`, or a `:=` whose value is
    one (final fix D5)."""
    if isinstance(expr, ast.NamedExpr):
        return holding(expr.value, holders)
    return is_max_ids(expr) or (isinstance(expr, ast.Name) and expr.id in holders)


def given_bounds(known: Mapping[str, Function]) -> dict[str, set[str]]:
    """Each function's parameters that a call in the module gives a manifest's bounds, whatever their names,
    to a fixpoint (a parameter so given passes them on)."""
    given: dict[str, set[str]] = {name: set() for name in known}
    changed = True
    while changed:
        changed = False
        for name, caller in known.items():
            holders = bounds_names(caller, given[name])
            for call in (node for node in ast.walk(caller.node) if isinstance(node, ast.Call)):
                callee = callee_of(call, caller, known)
                if callee is None:
                    continue
                target = known[callee]
                positional = [arg.arg for arg in (*target.node.args.posonlyargs, *target.node.args.args)]
                if target.owner is not None:
                    positional = positional[1:]  # self
                found = {
                    positional[at]
                    for at, arg in enumerate(call.args[: len(positional)])
                    if holding(arg, holders)
                }
                found |= {kw.arg for kw in call.keywords if kw.arg is not None and holding(kw.value, holders)}
                if not found <= given[callee]:
                    given[callee] |= found
                    changed = True
    return given


def escapes_in(
    function: Function, holders: set[str], known: Mapping[str, Function]
) -> Iterator[tuple[int, str]]:
    """The line and callee of every call in `function` that passes a manifest's bounds whole to a function
    outside the module."""
    for node in ast.walk(function.node):
        if not isinstance(node, ast.Call):
            continue
        passed = [*node.args, *(keyword.value for keyword in node.keywords)]
        if any(holding(arg, holders) for arg in passed) and callee_of(node, function, known) is None:
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else ast.unparse(node.func)
            yield node.lineno, callee


def reads_in(function: Function, holders: set[str]) -> Iterator[tuple[ast.expr, int, str]]:
    """The key of every bound read in `function`, with its line and its form (`Reads.forms`)."""
    for node in ast.walk(function.node):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and holding(node.value, holders)
        ):
            yield node.slice, node.lineno, "subscript"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and holding(node.func.value, holders)
            and node.args
        ):
            default = node.args[1] if len(node.args) > 1 else None
            zero = isinstance(default, ast.Constant) and default.value == 0 and not node.keywords
            yield node.args[0], node.lineno, "get_or_0" if zero else "get"


def bound_reads(tree: ast.Module, namespace: Mapping[str, Any], procedure: str) -> Reads:
    """The bound keys `procedure` (a class of the module) reads outside `compute` (module docstring)."""
    known = functions(tree)
    resolver = Resolver(namespace, known)
    given = given_bounds(known)
    given_contents = given_content(known)
    reads = Reads()
    for name in sorted(replay_reach(known, procedure)):
        holders = bounds_names(known[name], given[name])
        reads.unresolved += [
            f"{name}:{line} (unhandled use)" for line in unhandled_uses(known[name], holders, known)
        ]
        contents = content_names(known[name], given_contents[name])
        reads.unresolved += [
            f"{name}:{line} ({what})" for line, what in content_uses(known[name], contents, known)
        ]
        for line, callee in escapes_in(known[name], holders, known):
            if callee in PASSED_OUT:
                reads.keys |= read_by(PASSED_OUT[callee])
            else:
                reads.unresolved.append(f"{name}:{line} (passed out to {callee})")
        for key, line, form in reads_in(known[name], holders):
            resolved = resolver.key(key, known[name])
            if resolved is None:
                reads.unresolved.append(f"{name}:{line}")
                continue
            reads.keys |= resolved
            for one in resolved:
                reads.forms.setdefault(one, set()).add(form)
    return reads


def violations(reads: Reads, required: AbstractSet[str], added: AbstractSet[str]) -> list[str]:
    """What fails the check (module docstring): each unresolved read; each key the kind declares neither
    required nor added (`undeclared:<key>`); each added key read other than as `.get(key, 0)`
    (`added_read:<key>`), which an older manifest that lacks it would fail."""
    found = list(reads.unresolved)
    found += [f"undeclared:{key}" for key in sorted(reads.keys - required - added)]
    found += [f"added_read:{key}" for key in sorted(added & reads.keys) if reads.forms[key] != {"get_or_0"}]
    return found


def parsed(module: ModuleType) -> ast.Module:
    return ast.parse(inspect.getsource(module))


def procedure_of(module: ModuleType) -> Any:
    [procedure] = [proc for proc in KINDS.values() if type(proc).__module__ == module.__name__]
    return procedure


@pytest.mark.parametrize("module", MODULES, ids=[module.__name__.rsplit(".", 1)[1] for module in MODULES])
def test_every_bound_a_kind_reads_is_one_it_declares(module: ModuleType) -> None:
    procedure = procedure_of(module)
    reads = bound_reads(parsed(module), vars(module), type(procedure).__name__)
    assert violations(reads, procedure.max_ids_keys, procedure.max_ids_keys_added) == []
    # and the check sees the reads it is about: every key a kind declares is one it reads
    assert reads.keys == procedure.max_ids_keys | procedure.max_ids_keys_added


def test_every_kind_declares_its_added_keys_apart_from_its_required_ones() -> None:
    """Final fix C3: a key a release adds to what a kind reads is declared added (`max_ids_keys_added`, its
    absence read as 0: a table created after a manifest has no row within its bound), never required, or the
    release counts as a manifest-format change; no key is both, and registering a kind that declares one
    both is refused."""
    for kind, procedure in KINDS.items():
        assert isinstance(procedure.max_ids_keys_added, frozenset), kind
        assert procedure.max_ids_keys.isdisjoint(procedure.max_ids_keys_added), kind

    class Both:
        kind = "golden_case_delete"
        deletes_exports = True
        max_ids_keys = frozenset({"jobs"})
        max_ids_keys_added = frozenset({"jobs"})

    with pytest.raises(ValueError, match="both required and added"):
        register_kind(Both())  # type: ignore[arg-type]  # a partial procedure: refused before it is used
    assert KINDS["golden_case_delete"] is procedure_of(golden_case)


def planted(module: ModuleType, method: str, code: str, *, helper: str = "") -> ast.Module:
    """`module`'s tree with `code` at the start of the procedure's `method`, and `helper` (a function's
    source) added to the module."""
    tree = parsed(module)
    [cls] = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == type(procedure_of(module)).__name__
    ]
    [target] = [
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == method
    ]
    target.body[:0] = ast.parse(code).body
    tree.body.extend(ast.parse(helper).body)
    return tree


PLANTS: Final = {
    "a_subscript_in_execute": ("execute", 'content["max_ids"]["planted"]', "", (), ["undeclared:planted"]),
    "a_get_of_a_bound_name": (
        "execute",
        'planted_bounds = content["max_ids"]\nplanted_bounds.get("planted")',
        "",
        (),
        ["undeclared:planted"],
    ),
    "a_helper_the_postconditions_call": (
        "postconditions",
        '_planted(content["max_ids"])',
        "def _planted(bounds):\n    return bounds[PLANTED_KEY]",
        (),
        ["undeclared:planted"],
    ),
    "a_helper_whose_parameter_has_another_name": (
        "execute",
        '_planted(run, content["max_ids"])',
        "def _planted(run, limits):\n    return limits.get(PLANTED_KEY)",
        (),
        ["undeclared:planted"],
    ),
    "a_key_no_rule_resolves": (
        "execute",
        'content["max_ids"][str(len(content))]',
        "",
        (),
        # and the content given whole to a function outside the module (final fix E3)
        ["execute:1 (unhandled use of the content)", "execute:1"],
    ),
    "the_bounds_passed_out": (
        "execute",
        'elsewhere.read(content["max_ids"])',
        "",
        (),
        ["execute:1 (passed out to read)"],
    ),
    "an_added_key_read_by_subscript": (
        "execute",
        'content["max_ids"]["planted"]',
        "",
        ("planted",),
        ["added_read:planted"],
    ),
    "an_added_key_read_with_another_default": (
        "execute",
        'content["max_ids"].get("planted")',
        "",
        ("planted",),
        ["added_read:planted"],
    ),
    # the final re-review's R-M1: an annotated alias, and an alias of an alias (final fix C4)
    "an_annotated_alias": (
        "execute",
        'limits: dict[str, int] = content["max_ids"]\nlimits["planted"]',
        "",
        (),
        ["undeclared:planted"],
    ),
    "an_alias_of_an_alias": (
        "execute",
        'limits = content["max_ids"]\nother = limits\nother["planted"]',
        "",
        (),
        ["undeclared:planted"],
    ),
    # a use of the bounds that is no read the check can follow (final fix C4)
    "the_bounds_iterated": (
        "execute",
        'limits = content["max_ids"]\n[value for value in limits.values()]',
        "",
        (),
        ["execute:2 (unhandled use)"],
    ),
    "the_bounds_kept_on_an_attribute": (
        "execute",
        'run.kept = content["max_ids"]',
        "",
        (),
        ["execute:1 (unhandled use)"],
    ),
    # the final re-review's round-2 forms (final fix D5): the bounds taken by `.get`, with or without a
    # default; a computed key of the content; a subscripted `:=`
    "a_get_of_the_bounds": (
        "execute",
        'limits = content.get("max_ids")\nlimits["planted"]',
        "",
        (),
        ["undeclared:planted"],
    ),
    "a_get_of_the_bounds_with_a_default": (
        "execute",
        'content.get("max_ids", {})["planted"]',
        "",
        (),
        ["undeclared:planted"],
    ),
    "a_computed_key_of_the_content": (
        "execute",
        'k = "max_ids"\ncontent[k]["planted"]',
        "",
        (),
        ["execute:2 (computed key)"],
    ),
    "a_subscripted_walrus": (
        "execute",
        '(limits := content["max_ids"])["planted"]',
        "",
        (),
        ["undeclared:planted"],
    ),
    # the final re-review's round-3 forms (final fix E3, N-5): a computed key on an alias of the content, a
    # method of the content other than `.get`, and the content iterated
    "a_computed_key_on_an_alias_of_the_content": (
        "execute",
        'c = content\nk = "max_ids"\nc[k]["planted"]',
        "",
        (),
        ["execute:3 (computed key)"],
    ),
    "another_method_of_the_content": (
        "execute",
        'content.__getitem__("max_ids")["planted"]',
        "",
        (),
        ["execute:1 (unhandled use of the content)"],
    ),
    "the_content_iterated": (
        "execute",
        'for k, v in content.items():\n    v["planted"]',
        "",
        (),
        ["execute:1 (unhandled use of the content)"],
    ),
    # the final re-review's round-4 forms (R4-M1, R4-M2): an argument that binds to no named parameter of the
    # callee (its `*args` or `**kwargs`, or one after an unpacking) holds the content or the bounds where no
    # name the check follows does; a keyword argument that names a parameter is followed
    "the_content_into_a_variadic_parameter": (
        "execute",
        "_planted(content)",
        'def _planted(*args):\n    c = args[0]\n    k = "max_ids"\n    c[k]["planted"]',
        (),
        ["execute:1 (unhandled use of the content)"],
    ),
    "the_content_into_a_keyword_variadic_parameter": (
        "execute",
        "_planted(c=content)",
        'def _planted(**kwargs):\n    k = "max_ids"\n    return kwargs["c"][k]["planted"]',
        (),
        ["execute:1 (unhandled use of the content)"],
    ),
    "the_content_after_an_unpacking": (
        "execute",
        "_planted(*(), content)",
        'def _planted(c):\n    k = "max_ids"\n    return c[k]["planted"]',
        (),
        ["execute:1 (unhandled use of the content)"],
    ),
    "the_bounds_into_a_variadic_parameter": (
        "execute",
        '_planted(content["max_ids"])',
        "def _planted(*limits):\n    return limits[0][PLANTED_KEY]",
        (),
        ["execute:1 (unhandled use)"],
    ),
    "the_content_by_keyword": (
        "execute",
        "_planted(c=content)",
        'def _planted(c):\n    k = "max_ids"\n    return c[k]["planted"]',
        (),
        ["_planted:3 (computed key)"],
    ),
    "the_content_by_keyword_to_a_keyword_only_parameter": (
        "execute",
        "_planted(run, c=content)",
        'def _planted(run, *, c):\n    k = "max_ids"\n    return c[k]["planted"]',
        (),
        ["_planted:3 (computed key)"],
    ),
    "the_bounds_by_keyword": (
        "execute",
        '_planted(limits=content["max_ids"])',
        "def _planted(limits):\n    return limits[PLANTED_KEY]",
        (),
        ["undeclared:planted"],
    ),
    # accepted: a keyword call that reads a declared bound (R4-M2's control)
    "a_declared_bound_read_through_a_keyword_call": (
        "execute",
        "_planted(c=content)",
        'def _planted(c):\n    return c["max_ids"][TARGETS_BOUND]',
        (),
        [],
    ),
    # accepted: an added key read as 0 where an older manifest lacks it (final fix C3)
    "an_added_key_read_as_0_when_absent": (
        "execute",
        'content["max_ids"].get("planted", 0)',
        "",
        ("planted",),
        [],
    ),
    "a_read_in_compute_alone": ("compute", 'max_ids = {}\nmax_ids["planted"]', "", (), []),
}


@pytest.mark.parametrize(("method", "code", "helper", "added", "found"), PLANTS.values(), ids=PLANTS.keys())
def test_the_check_fails_on_a_planted_read(
    method: str, code: str, helper: str, added: tuple[str, ...], found: list[str]
) -> None:
    module = job_tombstone
    procedure = procedure_of(module)
    namespace = {**vars(module), "PLANTED_KEY": "planted"}
    name = type(procedure).__name__
    reads = bound_reads(planted(module, method, code, helper=helper), namespace, name)
    declared_added = procedure.max_ids_keys_added | frozenset(added)
    assert [
        v.removeprefix(f"{name}.") for v in violations(reads, procedure.max_ids_keys, declared_added)
    ] == found


def test_a_function_given_the_bounds_whole_reads_the_thread_owners() -> None:
    """`thread_bound_sql` reads the bound of each checkpoint thread form's owner, and nothing else."""
    assert read_by(manifests.thread_bound_sql) == {form.owner for form in manifests.THREAD_FORMS}
