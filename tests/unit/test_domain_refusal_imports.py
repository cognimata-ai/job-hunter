"""M4 Task 1 (R6; Global Constraints, API rules): the modules converted to `jobhunter.errors.Refusal` (the
settings save, the profile activation and the shipped-version activation) never import `jobhunter.api.errors`;
`jobhunter.api.models` (`DecimalOut`) stays allowed. An AST scan of each module's own import statements,
wherever they stand (a function's deferred import or a `TYPE_CHECKING` block included).
"""

import ast
from pathlib import Path
from typing import Final

SRC: Final = Path(__file__).resolve().parents[2] / "backend/src/jobhunter"
CONVERTED: Final = ("settings/save.py", "profile/activation.py", "settings/activate.py")
API_ERRORS: Final = "jobhunter.api.errors"


def _is_api_errors(module: str) -> bool:
    return module == API_ERRORS or module.startswith(f"{API_ERRORS}.")


def api_errors_imports(source: str) -> list[str]:
    """The imports of `jobhunter.api.errors` in `source`, in the order its statements name them: `import
    jobhunter.api.errors`, `from jobhunter.api.errors import Problem`, `from jobhunter.api import errors`."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if _is_api_errors(alias.name)]
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            if _is_api_errors(node.module):
                found.append(node.module)
            else:
                named = (f"{node.module}.{alias.name}" for alias in node.names)
                found += [module for module in named if _is_api_errors(module)]
    return found


def test_the_converted_modules_do_not_import_the_api() -> None:
    offending = {name: found for name in CONVERTED if (found := api_errors_imports((SRC / name).read_text()))}
    assert offending == {}


def test_every_spelling_of_an_api_errors_import_is_found_and_the_models_pass() -> None:
    source = "\n".join(
        [
            "from jobhunter.api.errors import FieldError, Problem",
            "import jobhunter.api.errors as api_errors",
            "from jobhunter.api import errors",
            "def later():",
            "    from jobhunter.api.errors import Problem",
            "from jobhunter.api.models import DecimalOut",
            "from jobhunter.api import models",
            "from jobhunter.errors import Refusal, RefusalField",
            "import jobhunter.api.errors_notes",
        ]
    )
    assert sorted(api_errors_imports(source)) == sorted(
        [API_ERRORS, API_ERRORS, API_ERRORS, API_ERRORS]  # the four spellings above, nothing else
    )
    # the pre-change line of all three modules is refused
    assert api_errors_imports("from jobhunter.api.errors import FieldError, Problem\n") == [API_ERRORS]
