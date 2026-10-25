"""The AST half of the LLM boundary (spec §10.1; plan Task 5, F24), run by CI's `lint` job.

ruff's `TID251` bans constructing a Claude client outside `backend/src/jobhunter/llm/**`; this check fails the
build on the request features the gateway never sends, wherever they appear:
- `bind_tools`, `tool_choice`, `temperature`, `top_p` or `top_k` as a name, an attribute or a keyword argument
  (`ast.Name`, `ast.Attribute`, `ast.keyword`; `banned_name`). A string constant naming one passes, so the
  tests and spikes that assert those keys are absent from a request body (`"temperature" not in body`) pass;
- `with_structured_output` other than called with `method="json_schema"` as a literal keyword
  (`structured_output_method`): no other method, no method from `**kwargs`, no bare reference;
- an `AIMessage(...)` as the last element of a list or tuple literal: an assistant prefill
  (`assistant_prefill`). The bare class, as in `isinstance(raw, (HumanMessage, AIMessage))`, is no message.

Usage: `uv run python scripts/check_llm_boundary.py [path ...]`; without paths it checks every `.py` file
under `DEFAULT_ROOTS` of the repository. It prints one `<path>:<line>: <rule> <name>` line per violation and
exits 1 when there is any.
"""

import ast
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS: Final = ("backend", "scripts", "tests", "embedder", "docparse")
BANNED: Final = frozenset({"bind_tools", "tool_choice", "temperature", "top_p", "top_k"})
STRUCTURED_OUTPUT: Final = "with_structured_output"
_SKIPPED_DIRS: Final = frozenset({".venv", "node_modules", "__pycache__", ".git"})


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    name: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.name}"


def _name_of(node: ast.expr) -> str | None:
    """`x` for `x` and `a.b.x`, else `None`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _json_schema_method(call: ast.Call) -> bool:
    """Whether `call` passes `method="json_schema"` literally and no `**kwargs` that could replace it."""
    if any(keyword.arg is None for keyword in call.keywords):
        return False
    return any(
        keyword.arg == "method"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value == "json_schema"
        for keyword in call.keywords
    )


def _is_ai_message(node: ast.expr) -> bool:
    """A constructed `AIMessage(...)`; the bare class (a type in `isinstance(x, (A, AIMessage))`) is none."""
    return isinstance(node, ast.Call) and _name_of(node.func) == "AIMessage"


class _Checker(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.violations: list[Violation] = []
        self._checked_calls: set[int] = set()  # the `func` nodes of well-formed structured-output calls

    def _add(self, node: ast.AST, rule: str, name: str) -> None:
        self.violations.append(Violation(self.path, getattr(node, "lineno", 0), rule, name))

    def visit_Name(self, node: ast.Name) -> None:
        self._identifier(node, node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._identifier(node, node.attr)
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg in BANNED:
            self._add(node.value, "banned_name", node.arg)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _name_of(node.func) == STRUCTURED_OUTPUT and _json_schema_method(node):
            self._checked_calls.add(id(node.func))
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        self._sequence(node.elts)
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._sequence(node.elts)
        self.generic_visit(node)

    def _identifier(self, node: ast.expr, name: str) -> None:
        if name in BANNED:
            self._add(node, "banned_name", name)
        elif name == STRUCTURED_OUTPUT and id(node) not in self._checked_calls:
            self._add(node, "structured_output_method", name)

    def _sequence(self, elements: Sequence[ast.expr]) -> None:
        if elements and _is_ai_message(elements[-1]):
            self._add(elements[-1], "assistant_prefill", "AIMessage")


def check_source(source: str, path: str) -> list[Violation]:
    """The violations in the Python `source` of `path`, in source order."""
    checker = _Checker(path)
    checker.visit(ast.parse(source, filename=path))
    return sorted(checker.violations, key=lambda v: (v.line, v.rule, v.name))


def _python_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_file():
            yield path
        elif path.is_dir():
            for found in sorted(path.rglob("*.py")):
                if not _SKIPPED_DIRS.intersection(found.relative_to(path).parts):
                    yield found


def check_paths(paths: Iterable[Path]) -> list[Violation]:
    """The violations of every `.py` file in `paths` (files, or directories searched recursively)."""
    violations: list[Violation] = []
    for file in _python_files(paths):
        violations.extend(check_source(file.read_text("utf-8"), str(file)))
    return violations


def main(argv: Sequence[str]) -> int:
    paths = [Path(arg) for arg in argv] or [REPO / root for root in DEFAULT_ROOTS]
    violations = check_paths(paths)
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
