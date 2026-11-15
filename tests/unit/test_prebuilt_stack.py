"""The prebuilt stack path (`tests/support/prebuilt.py`; F6 fix round 1, Ruling F6-2), against a fake `docker`
on PATH that records its arguments, answers `config --format json` with a compose model and `image inspect`
with FAKE_IMAGE_STATUS: with services named prebuilt, the start checks the image each one uses (named by the
model) before anything else, fails naming the service and the image when one is missing, builds only the
others, and starts with `up --no-build`, so compose never builds a prebuilt image."""

import json
import os
from pathlib import Path
from typing import Any, Final

import pytest

from tests.support import prebuilt

ARGV: Final = ["docker", "compose", "-p", "prebuilt-test"]
BUILT: Final = ("migrate", "api", "worker", "embedder", "docparse")
# The embedder's image as the fake compose model names it: no tag the helper could know by itself.
IMAGE: Final = "registry.invalid/embedder-under-test:7"
MODEL: Final[dict[str, Any]] = {
    "name": "prebuilt-test",
    "services": {
        "embedder": {"image": IMAGE, "build": {"context": ".", "target": "runtime"}},
        "worker": {"image": "jobhunter-app:dev", "build": {"context": "."}},
        "postgres": {"image": "pgvector/pgvector:pinned"},
    },
}
FAKE_DOCKER: Final = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DIR/calls"
case "$*" in
  *" config --format json") cat "$FAKE_DIR/model.json" ;;
  "image inspect "*) exit "$FAKE_IMAGE_STATUS" ;;
esac
exit 0
"""


class FakeDocker:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def calls(self) -> list[str]:
        path = self.directory / "calls"
        return path.read_text("utf-8").splitlines() if path.exists() else []


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER, "utf-8")
    (bin_dir / "docker").chmod(0o755)
    (tmp_path / "model.json").write_text(json.dumps(MODEL), "utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    monkeypatch.setenv("FAKE_IMAGE_STATUS", "0")
    return FakeDocker(tmp_path)


def test_a_prebuilt_start_checks_the_image_then_builds_the_others_and_starts_without_building(
    fake_docker: FakeDocker,
) -> None:
    done = prebuilt.start(ARGV, BUILT, ("embedder",), timeout_s=60)
    assert done.returncode == 0
    assert fake_docker.calls() == [
        "compose -p prebuilt-test config --format json",
        f"image inspect --format {{{{.Id}}}} {IMAGE}",
        "compose -p prebuilt-test build migrate api worker docparse",
        "compose -p prebuilt-test up -d --wait --no-build",
    ]


def test_a_missing_prebuilt_image_fails_the_start_naming_the_service_and_the_image(
    fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_IMAGE_STATUS", "1")
    with pytest.raises(pytest.fail.Exception) as failed:
        prebuilt.start(ARGV, BUILT, ("embedder",), timeout_s=60)
    message = str(failed.value)
    assert f"`embedder` uses `{IMAGE}`" in message
    assert "must be prebuilt" in message
    assert ".github/workflows/embedder-image.yml" in message
    # nothing was built or started: the check comes first, and compose never sees the missing image
    assert [call for call in fake_docker.calls() if " build " in f"{call} " or " up " in call] == []


def test_without_prebuilt_services_the_start_builds_every_image(fake_docker: FakeDocker) -> None:
    prebuilt.start(ARGV, BUILT, (), timeout_s=60)
    assert fake_docker.calls() == ["compose -p prebuilt-test up -d --build --wait"]


def test_the_images_come_from_the_compose_model_and_names_outside_it_are_ignored() -> None:
    model = {"name": "proj", "services": {"embedder": {"image": "x/embedder:1"}, "worker": {"build": {}}}}
    # `worker` has no `image`: compose names its build `<project>-<service>`; `backup` is no service here
    assert prebuilt.images_of(model, ("embedder", "worker", "backup")) == {
        "embedder": "x/embedder:1",
        "worker": "proj-worker",
    }
