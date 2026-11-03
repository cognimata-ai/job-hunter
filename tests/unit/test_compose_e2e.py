"""The E2E stack's overlay (`deploy/compose.e2e.yaml`; M4 Task 18): the embedder on its image's `fake` target
with `EMBEDDER_FAKE` in dev, the worker's two fake-LLM stack controls on its `/tmp` tmpfs, and its header that
keeps it out of prod."""

from pathlib import Path
from typing import Any, Final

import yaml

DEPLOY: Final = Path(__file__).parents[2] / "deploy"
OVERLAY: Final = DEPLOY / "compose.e2e.yaml"
# Paths inside the worker container, on its tmpfs: not temporary files of this process.
TMPFS: Final = "/tmp"  # noqa: S108
SCRIPT_FILE: Final = f"{TMPFS}/fake-batch-script.json"
KEYED_DIR: Final = f"{TMPFS}/fake-keyed"


def _services(path: Path) -> dict[str, Any]:
    services: dict[str, Any] = yaml.safe_load(path.read_text("utf-8"))["services"]
    return services


def test_the_overlay_puts_the_embedder_on_the_fake_target_and_the_worker_on_its_tmpfs_controls() -> None:
    services = _services(OVERLAY)
    assert set(services) == {"embedder", "worker"}
    embedder = services["embedder"]
    assert (embedder["image"], embedder["build"]) == ("jobhunter-embedder:e2e", {"target": "fake"})
    assert embedder["environment"] == {"EMBEDDER_FAKE": "1", "JOBHUNTER_ENV": "dev"}
    assert services["worker"] == {
        "environment": {
            "JOBHUNTER_FAKE_BATCH_SCRIPT": SCRIPT_FILE,
            "JOBHUNTER_FAKE_LLM_KEYED_DIR": KEYED_DIR,
        }
    }
    # both paths are on the worker's tmpfs (the dev stack's `x-app` anchor), a read-only container otherwise
    worker = _services(DEPLOY / "compose.dev.yaml")["worker"]
    assert worker["tmpfs"] == [TMPFS] and worker["read_only"] is True
    # the real embedder image keeps its own tag
    assert _services(DEPLOY / "compose.dev.yaml")["embedder"]["image"] == "jobhunter-embedder:dev"


def test_the_overlay_says_first_that_it_is_never_for_prod() -> None:
    first = OVERLAY.read_text("utf-8").splitlines()[0]
    assert first.startswith("# Dev and CI only, never in prod")
