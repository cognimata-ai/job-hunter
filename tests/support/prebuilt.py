"""The start of a stack whose services `JOBHUNTER_STACK_PREBUILT` may name (tests/stack/test_dev_stack.py;
F6 fix round 1, Ruling F6-2).

`JOBHUNTER_STACK_PREBUILT` names compose services, comma-separated (e.g. `embedder`), whose existing local
image a stack run uses as it is and never builds: for a host that can reach PyPI but not every build source of
an image (the real embedder's Debian mirror and model download). Compose builds the missing image of a service
that has a `build:` section on `up` (unless `--no-build`) and on `run` (which has no such flag), so a missing
prebuilt image would be built after all. `start` therefore first checks that the image each prebuilt service
uses exists locally (`require_prebuilt`: the image the project's own compose model names, read with
`config --format json`, never a tag known here), and fails naming the service and the image when one does not,
before anything is built or started; then it builds the other services and starts the stack with
`up -d --wait --no-build`. A later `run` of the started stack meets only images that exist. Without prebuilt
services every image is built (`up -d --build --wait`), as always.
"""

import json
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, Final

import pytest

CALL_TIMEOUT_S: Final = 180
# Where a prebuilt image comes from, by service, for the failure's message.
SOURCES: Final = {
    "embedder": "the real embedder image is built by CI's .github/workflows/embedder-image.yml (its model is "
    "downloaded at build), on a host that reaches its build sources",
}


def images_of(model: Mapping[str, Any], services: Sequence[str]) -> dict[str, str]:
    """The image each of `services` uses in the compose `model` (`config --format json`), by service. A name
    that is no service of the model is left out (the variable is shared by every stack project); a service
    without `image` is built as `<project>-<service>`, compose's own name for it."""
    found = model.get("services") or {}
    return {name: found[name].get("image") or f"{model['name']}-{name}" for name in services if name in found}


def missing_message(missing: Mapping[str, str]) -> str:
    """The failure that names each missing prebuilt image's service and image."""
    lines = [
        f"`{service}` uses `{image}`, which does not exist here; it must be prebuilt"
        + (f" ({SOURCES[service]})" if service in SOURCES else "")
        for service, image in missing.items()
    ]
    return (
        "JOBHUNTER_STACK_PREBUILT names services this run never builds, and compose would build a missing "
        "one:\n"
        + "\n".join(lines)
        + "\nBuild it first, or run without JOBHUNTER_STACK_PREBUILT on a host that reaches its build "
        "sources."
    )


def image_exists(image: str) -> bool:
    found = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True,
        text=True,
        check=False,
        timeout=CALL_TIMEOUT_S,
    )
    return found.returncode == 0


def require_prebuilt(argv: Sequence[str], prebuilt: Sequence[str]) -> None:
    """Fails (`pytest.fail`) unless the image of each prebuilt service of the project `argv` exists
    locally."""
    config = subprocess.run(
        [*argv, "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=CALL_TIMEOUT_S,
    )
    if config.returncode != 0:
        pytest.fail(f"docker compose config failed ({config.returncode}):\n{config.stderr}")
    images = images_of(json.loads(config.stdout), prebuilt)
    missing = {service: image for service, image in images.items() if not image_exists(image)}
    if missing:
        pytest.fail(missing_message(missing))


def start(
    argv: Sequence[str], built_services: Sequence[str], prebuilt: Sequence[str], *, timeout_s: float
) -> subprocess.CompletedProcess[bytes]:
    """The stack `argv` started (`up -d --wait`), its images built first but never a prebuilt one (module
    docstring); the `up`'s result, for the caller to judge."""
    if not prebuilt:
        return subprocess.run([*argv, "up", "-d", "--build", "--wait"], check=False, timeout=timeout_s)
    require_prebuilt(argv, prebuilt)
    if built := [name for name in built_services if name not in prebuilt]:
        subprocess.run([*argv, "build", *built], check=True, timeout=timeout_s)
    return subprocess.run([*argv, "up", "-d", "--wait", "--no-build"], check=False, timeout=timeout_s)
