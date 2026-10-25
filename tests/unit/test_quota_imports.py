"""M1 Carry-forward 13: `quota/` builds no adapter. No module under `quota/` imports the adapter factory
(`jobhunter.sources.registry`, `make_adapter`) or a concrete adapter module (`jobhunter.sources.{jsearch,
serpapi,adzuna,arbeitnow}` and `jobhunter.sources.ats.*`); the planning-time adapters are
`sources.spec_adapters`', the cells `quota.cells`', and `CURSOR_REUSE_TTL` is `sources.coverage`'s, which
`quota/` may import. An AST scan of each module's own import statements, wherever they stand (a function's
deferred import or a `TYPE_CHECKING` block included); what a permitted module imports in turn is not
`quota/`'s.
"""

import ast
from pathlib import Path
from typing import Final

QUOTA: Final = Path(__file__).resolve().parents[2] / "backend/src/jobhunter/quota"
_SOURCES: Final = "jobhunter.sources"
FORBIDDEN: Final = frozenset(
    f"{_SOURCES}.{name}" for name in ("registry", "jsearch", "serpapi", "adzuna", "arbeitnow", "ats")
)


def _forbidden(module: str) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in FORBIDDEN)


def forbidden_imports(source: str) -> list[str]:
    """The forbidden modules `source` imports, in the order its statements name them: `import a.b`, `from a.b
    import c`, and `from jobhunter.sources import jsearch` (a module imported by its name)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if _forbidden(alias.name)]
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            if _forbidden(node.module):
                found.append(node.module)
            else:
                named = (f"{node.module}.{alias.name}" for alias in node.names)
                found += [module for module in named if _forbidden(module)]
    return found


def test_no_quota_module_imports_the_adapter_factory_or_an_adapter() -> None:
    modules = sorted(QUOTA.rglob("*.py"))
    assert {path.name for path in modules} >= {"planner.py", "cells.py", "ledger.py"}
    offending = {
        str(path.relative_to(QUOTA)): found
        for path in modules
        if (found := forbidden_imports(path.read_text(encoding="utf-8")))
    }
    assert offending == {}


def test_the_pre_refactor_planner_imports_are_refused() -> None:
    """The two lines `planner.py` had before the split: the check refuses both."""
    assert forbidden_imports("from jobhunter.sources.registry import make_adapter\n") == [
        "jobhunter.sources.registry"
    ]
    assert forbidden_imports("from jobhunter.sources.jsearch import CURSOR_REUSE_TTL\n") == [
        "jobhunter.sources.jsearch"
    ]


def test_every_spelling_of_a_forbidden_import_is_found_and_the_permitted_ones_pass() -> None:
    source = "\n".join(
        [
            "import jobhunter.sources.serpapi",
            "from jobhunter.sources import adzuna, coverage",
            "from jobhunter.sources.ats.greenhouse import GreenhouseAdapter",
            "from jobhunter.sources import ats",
            "def later():",
            "    from jobhunter.sources.arbeitnow import ArbeitnowAdapter",
            "from jobhunter.sources.coverage import CURSOR_REUSE_TTL",
            "from jobhunter.sources.spec_adapters import _spec_adapters",
            "from jobhunter.sources.base import SourceAdapter",
            "import jobhunter.sources.registry_notes",
        ]
    )
    assert sorted(forbidden_imports(source)) == sorted(
        [
            "jobhunter.sources.serpapi",
            "jobhunter.sources.adzuna",
            "jobhunter.sources.ats.greenhouse",
            "jobhunter.sources.ats",
            "jobhunter.sources.arbeitnow",
        ]
    )
