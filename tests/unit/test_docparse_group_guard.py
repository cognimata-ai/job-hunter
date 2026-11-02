"""The docparse group's guard (`tests.support.profile.docparse_group_missing`; M3 Task 28, carry T18-b).

A docparse child is a fresh interpreter: it imports pypdf, pdfplumber and python-docx from the environment
when it extracts, and a module it cannot import is its `parse_error`, silently (`docparse.worker._child`).
The session-start hook's `uv sync --frozen` removes the `docparse` group from the environment while a run
that started with `uv run --group docparse` goes on, so the guard must read the environment as such a child
would, never this process's `sys.modules`: collecting `test_profile_extract_phase.py` imports pypdf here (an
`encrypted_pdf` parameter).
"""

import ast
import shutil
import sys
from pathlib import Path

import pytest

from tests.support import profile
from tests.support.profile import DOCPARSE_GROUP_MODULES, docparse_group_missing

EXTRACTORS = Path(__file__).resolve().parents[2] / "docparse" / "extract.py"
PROBE = "jh_t18b_probe"


def probe_package(root: Path) -> Path:
    package = root / PROBE
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    return package


def runtime_imports(path: Path) -> set[str]:
    """The top-level modules `path` imports outside `if TYPE_CHECKING:`, the standard library left out."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guards = [b for b in ast.walk(tree) if isinstance(b, ast.If) and ast.unparse(b.test) == "TYPE_CHECKING"]
    typing_only = {id(node) for branch in guards for node in ast.walk(branch)}
    names: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            names.add(node.module.partition(".")[0])
    return {name for name in names if name not in sys.stdlib_module_names and name != "docparse"}


def test_the_guard_names_every_module_the_extractors_import() -> None:
    assert set(DOCPARSE_GROUP_MODULES) == runtime_imports(EXTRACTORS) == {"pypdf", "pdfplumber", "docx"}


def test_a_module_this_process_loaded_but_the_environment_lost_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(profile, "DOCPARSE_GROUP_MODULES", (PROBE,))
    monkeypatch.syspath_prepend(str(tmp_path))
    package = probe_package(tmp_path)
    try:
        __import__(PROBE)  # as collection imports pypdf into the test process
        assert docparse_group_missing() == []
        shutil.rmtree(package)  # as `uv sync --frozen` uninstalls the group mid-run
        assert PROBE in sys.modules  # this process still holds it; a fresh interpreter cannot import it
        assert docparse_group_missing() == [PROBE]
    finally:
        sys.modules.pop(PROBE, None)


def test_a_module_on_disk_is_found_without_being_imported_and_an_absent_one_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(profile, "DOCPARSE_GROUP_MODULES", (PROBE, "jh_t18b_never_installed"))
    monkeypatch.syspath_prepend(str(tmp_path))
    probe_package(tmp_path)
    assert docparse_group_missing() == ["jh_t18b_never_installed"]
    assert PROBE not in sys.modules
