"""No shipped image holds pip (plan M5 Task 29, Rulings T29-3 and T29-7; M5 follow-ups Task F2): every stage
that becomes an image, the stages that switch to a non-root user, builds on a stage of the python base image
that removes the base image's pip: its package, its dist-info (whose SBOM lists the packages pip vendors,
certifi among them, which the image scan reads) and its scripts. Nothing runs pip: uv installs each venv in a
build stage. The `build`, `model` and `spa` stages never ship, and keep what their base has.

The images themselves are checked on the compose test stack (`tests/stack/test_prod_stack.py`)."""

import re
from pathlib import Path
from typing import Final

import pytest

REPO: Final = Path(__file__).resolve().parents[2]
DOCKERFILES: Final = ("Dockerfile", "docparse/Dockerfile", "embedder/Dockerfile")
PYTHON_BASE: Final = "${PYTHON_IMAGE}"
# Each shipped stage with the stage on the python base it builds on: the app's backup stage builds on its
# runtime, and the embedder's test-only `fake` target is the runtime without the model (M4 Task 18).
SHIPPED: Final = {
    ("Dockerfile", "runtime"): "runtime",
    ("Dockerfile", "backup"): "runtime",
    ("docparse/Dockerfile", "runtime"): "runtime",
    ("embedder/Dockerfile", "fake"): "fake",
    ("embedder/Dockerfile", "runtime"): "runtime",
}
PIP_PATHS: Final = (
    "/usr/local/lib/python3*/site-packages/pip",
    "/usr/local/lib/python3*/site-packages/pip-*.dist-info",
    "/usr/local/bin/pip*",
)
_FROM: Final = re.compile(r"^FROM (\S+) AS (\w+)\n", re.MULTILINE)


def stages(path: str) -> dict[str, tuple[str, list[str]]]:
    """Each named stage of the Dockerfile: its base and its instructions, continuation lines joined and
    comments dropped."""
    parts = _FROM.split((REPO / path).read_text(encoding="utf-8"))
    found = {}
    for base, name, body in zip(parts[1::3], parts[2::3], parts[3::3], strict=True):
        lines = body.replace("\\\n", " ").splitlines()
        found[name] = (base, [line for line in lines if line.strip() and not line.lstrip().startswith("#")])
    return found


def python_root(path: str, stage: str) -> str:
    """The stage on the python base that `stage` builds on, through the stages of the same file."""
    named = stages(path)
    while (base := named[stage][0]) != PYTHON_BASE:
        stage = base
    return stage


def removed(lines: list[str]) -> set[str]:
    """The arguments of every `rm -rf` command of the stage's RUN instructions."""
    commands = (
        command.split()
        for line in lines
        if line.startswith("RUN ")
        for command in re.split(r"&&|;", line.removeprefix("RUN "))
    )
    return {word for words in commands if words[:2] == ["rm", "-rf"] for word in words[2:]}


def test_the_shipped_stages_are_those_named() -> None:
    shipped = {
        (path, name)
        for path in DOCKERFILES
        for name, (_, lines) in stages(path).items()
        if any(line.startswith("USER ") for line in lines)
    }
    assert shipped == set(SHIPPED)
    assert {key: python_root(*key) for key in shipped} == SHIPPED


@pytest.mark.parametrize(("path", "stage"), sorted({(path, root) for (path, _), root in SHIPPED.items()}))
def test_each_python_stage_that_ships_removes_pip(path: str, stage: str) -> None:
    base, lines = stages(path)[stage]
    assert base == PYTHON_BASE
    assert set(PIP_PATHS) <= removed(lines), (path, stage)
