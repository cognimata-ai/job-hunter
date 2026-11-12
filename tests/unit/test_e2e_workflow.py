"""The CI `e2e` job (`.github/workflows/e2e.yml`; plan M4 Task 32, U2, R3, Rulings T32-a, F4-a): on pull
requests to main and by hand only; the spec list checked before a browser, the stack or the suite; the
sentinel check that no failure artefact keeps a credential (`e2e:sentinel`) run once the browser is installed
and before the stack; the shipper's image built before the stack comes up (E18 runs it, plan M5 Task 30);
every action pinned to a full commit SHA (checkout, setup-uv and setup-node as `ci.yml` pins them,
upload-artifact as `eval-manual.yml` does); the report and the test output uploaded on a failure only, the
stack and its volumes removed in every case; and no secret referenced."""

import re
from pathlib import Path
from typing import Any, Final

import yaml

WORKFLOWS: Final = Path(__file__).resolve().parents[2] / ".github/workflows"
WORKFLOW: Final = WORKFLOWS / "e2e.yml"
_USES: Final = re.compile(r"uses: ([\w.-]+/[\w.-]+)@(\S+)( # \S+)?")
_PINNED: Final = re.compile(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}")
STACK_FILES: Final = (
    "deploy/compose.dev.yaml",
    "deploy/compose.fixtures.yaml",
    "deploy/compose.fakellm.yaml",
    "deploy/compose.e2e.yaml",
)


def workflow() -> dict[Any, Any]:
    doc: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text("utf-8"))
    return doc


def triggers(doc: dict[Any, Any]) -> Any:
    return doc["on"] if "on" in doc else doc[True]  # YAML 1.1 reads a bare `on` as true


def steps() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = workflow()["jobs"]["e2e"]["steps"]
    return found


def index_of(command: str) -> int:
    """The index of the one step whose `run` is `command`."""
    (found,) = [i for i, step in enumerate(steps()) if str(step.get("run", "")).strip() == command]
    return found


def pins(path: Path) -> dict[str, str]:
    """Each action's `<sha> <comment>` in a workflow file (one pin per action)."""
    found: dict[str, set[str]] = {}
    for action, ref, comment in _USES.findall(path.read_text("utf-8")):
        found.setdefault(action, set()).add(f"{ref}{comment}")
    assert all(len(refs) == 1 for refs in found.values()), found
    return {action: refs.pop() for action, refs in found.items()}


def test_triggers_are_pull_requests_to_main_and_dispatch_only() -> None:
    doc = workflow()
    assert triggers(doc) == {"pull_request": {"branches": ["main"]}, "workflow_dispatch": {}}
    assert doc["permissions"] == {"contents": "read"}
    assert list(doc["jobs"]) == ["e2e"]
    job = doc["jobs"]["e2e"]
    assert (job["runs-on"], job["timeout-minutes"]) == ("ubuntu-24.04", 75)
    stack = job["env"]["DC"].split()
    assert stack[:4] == ["docker", "compose", "--env-file", "deploy/images.lock"]
    assert [stack[i + 1] for i, arg in enumerate(stack) if arg == "-f"] == list(STACK_FILES)


def test_the_spec_list_is_checked_before_the_run() -> None:
    order = [
        index_of("uv sync --frozen"),
        index_of("npm ci --prefix frontend"),
        index_of("npm --prefix frontend run e2e:check"),
        index_of("npx --prefix frontend playwright install --with-deps chromium"),
        index_of("npm --prefix frontend run e2e:sentinel"),
        index_of("sudo scripts/dev-secrets.sh"),
        index_of("$DC --profile shipper build"),  # E18 runs the shipper (plan M5 Task 30)
        index_of("$DC up -d --build --wait"),
        index_of("npm --prefix frontend run e2e"),
    ]
    assert order == sorted(order)
    # nothing installs a browser, builds the stack or runs a spec before the list is checked
    early = steps()[: index_of("npm --prefix frontend run e2e:check")]
    assert not any(
        word in str(step.get("run", "")) for step in early for word in ("playwright", "$DC", "e2e")
    )


def test_every_action_is_pinned_to_a_sha() -> None:
    used = [str(step["uses"]) for step in steps() if "uses" in step]
    assert used and all(_PINNED.fullmatch(action) for action in used), used
    mine, ci, manual = pins(WORKFLOW), pins(WORKFLOWS / "ci.yml"), pins(WORKFLOWS / "eval-manual.yml")
    for action in ("actions/checkout", "astral-sh/setup-uv", "actions/setup-node"):
        assert mine[action] == ci[action], action
    assert mine["actions/upload-artifact"] == manual["actions/upload-artifact"]  # Ruling T32-a
    assert set(mine) == {
        "actions/checkout",
        "astral-sh/setup-uv",
        "actions/setup-node",
        "actions/upload-artifact",
    }


def test_artifacts_upload_on_failure_only() -> None:
    found = steps()
    (upload,) = [step for step in found if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    assert upload["if"] == "failure()"
    assert upload["with"]["path"].split() == ["frontend/playwright-report", "frontend/test-results"]
    assert found.index(upload) > index_of("npm --prefix frontend run e2e")
    down = found[index_of("$DC down -v")]
    assert down["if"] == "always()" and found[-1] is down  # the stack and its volumes go last, in every case
    assert [step for step in found if "if" in step] == [upload, down]


def test_no_secret_is_referenced() -> None:
    text = WORKFLOW.read_text("utf-8")
    assert not re.search(r"\$\{\{[^}]*secrets", text) and "sk-ant" not in text
    assert "--preserve-env" not in text  # dev-secrets.sh gets no key: the fake LLM needs none
    assert all("persist-credentials" in str(step.get("with", {})) for step in steps()[:1])
    env = workflow()["jobs"]["e2e"]["env"]
    assert set(env) == {"DC"}
