"""The prebuilt stack paths (`tests/support/prebuilt.py`, and `tests/support/prodstack.py`'s `up`; F6 fix
rounds 1 and 2, Rulings F6-2 and F6-4), against a fake `docker` on PATH that records its arguments, answers
`config --format json` with a compose model and `image inspect` with FAKE_IMAGE_STATUS: with services named
prebuilt, a start checks the image each one uses (named by the model) before anything else, fails naming the
service and the image when one is missing and naming a name that is no service, builds only the services whose
image is no prebuilt one (a service that shares a prebuilt service's tag would rebuild it), and starts with
`up --no-build`, so compose never builds a prebuilt image."""

import json
import os
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from tests.support import prebuilt, prodstack

REPO: Final = Path(__file__).resolve().parents[2]
ARGV: Final = ["docker", "compose", "-p", "prebuilt-test"]
BUILT: Final = ("migrate", "api", "worker", "embedder", "docparse")
# The embedder's image as the fake compose model names it: no tag the helper could know by itself.
IMAGE: Final = "registry.invalid/embedder-under-test:7"
APP: Final = "jobhunter-app:dev"
# migrate, api and worker share one image and one build, as compose.dev.yaml's and compose.test.yaml's do.
MODEL: Final[dict[str, Any]] = {
    "name": "prebuilt-test",
    "services": {
        "migrate": {"image": APP, "build": {"context": "."}},
        "api": {"image": APP, "build": {"context": "."}},
        "worker": {"image": APP, "build": {"context": "."}},
        "backup": {"image": "jobhunter-backup:dev", "build": {"context": ".", "target": "backup"}},
        "restore": {"image": "jobhunter-backup:dev", "build": {"context": ".", "target": "backup"}},
        "docparse": {"image": "jobhunter-docparse:dev", "build": {"context": "docparse"}},
        "embedder": {"image": IMAGE, "build": {"context": ".", "target": "runtime"}},
        "postgres": {"image": "pgvector/pgvector:pinned"},
    },
}
CONFIG: Final = "compose -p prebuilt-test --profile * config --format json"
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
        CONFIG,
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


def test_a_service_sharing_a_prebuilt_image_is_not_built(fake_docker: FakeDocker) -> None:
    """F6 fix round 2 (Codex I2, Ruling F6-4): `api` prebuilt, so `migrate` and `worker`, which build the same
    tag, are not built either: their build would replace the prebuilt image."""
    prebuilt.start(ARGV, BUILT, ("api",), timeout_s=60)
    assert fake_docker.calls() == [
        CONFIG,
        f"image inspect --format {{{{.Id}}}} {APP}",
        "compose -p prebuilt-test build embedder docparse",
        "compose -p prebuilt-test up -d --wait --no-build",
    ]


def test_the_dev_stacks_shared_app_image_is_never_built_when_one_of_its_services_is_prebuilt() -> None:
    """The same rule on compose.dev.yaml itself (its `x-app` gives migrate, api and worker one image): naming
    any one of them prebuilt leaves all three unbuilt, and the embedder alone leaves the app services
    built."""
    services = yaml.safe_load((REPO / "deploy/compose.dev.yaml").read_text("utf-8"))["services"]
    model = {"name": "jobhunter-dev", "services": services}
    for name in ("migrate", "api", "worker"):
        assert prebuilt.to_build(model, BUILT, (name,)) == ["embedder", "docparse"], name
    assert prebuilt.to_build(model, BUILT, ("embedder",)) == ["migrate", "api", "worker", "docparse"]


def test_a_prebuilt_name_that_is_no_service_fails_by_name() -> None:
    """F6 fix round 2 (Ruling F6-4): a name that is no service of the model, such as a misspelling, fails
    naming it, instead of protecting nothing."""
    with pytest.raises(pytest.fail.Exception) as failed:
        prebuilt.images_of(MODEL, ("embedder", "embeder"))
    message = str(failed.value)
    assert "JOBHUNTER_STACK_PREBUILT names `embeder`, which is no service of this stack" in message
    assert "`embedder`" in message  # the services it has


def test_a_service_without_an_image_is_named_as_compose_names_its_build() -> None:
    model = {"name": "proj", "services": {"embedder": {"image": "x/embedder:1"}, "worker": {"build": {}}}}
    # `worker` has no `image`: compose names its build `<project>-<service>`
    assert prebuilt.images_of(model, ("embedder", "worker")) == {
        "embedder": "x/embedder:1",
        "worker": "proj-worker",
    }


def test_the_prod_stack_checks_the_prebuilt_images_and_never_builds_their_tag(
    fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6 fix round 2 (Ruling F6-4): `prodstack.up` checks the prebuilt images by name as the dev path does,
    builds neither them nor a service that shares their tag, and starts with `up --no-build`."""
    monkeypatch.setattr(prodstack, "PREBUILT", ("api",))
    prodstack.up(ARGV)
    assert fake_docker.calls() == [
        CONFIG,
        f"image inspect --format {{{{.Id}}}} {APP}",
        "compose -p prebuilt-test build backup restore docparse embedder",
        "compose -p prebuilt-test up -d --no-build --wait caddy api worker docparse stubsvc backup",
    ]


def test_the_prod_stack_fails_by_name_on_a_missing_prebuilt_image_before_anything_starts(
    fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prodstack, "PREBUILT", ("embedder",))
    monkeypatch.setenv("FAKE_IMAGE_STATUS", "1")
    with pytest.raises(pytest.fail.Exception) as failed:
        prodstack.up(ARGV, build=False)
    assert f"`embedder` uses `{IMAGE}`, which does not exist here; it must be prebuilt" in str(failed.value)
    assert [call for call in fake_docker.calls() if " build " in f"{call} " or " up " in call] == []
