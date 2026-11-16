"""The images a stack run uses as they are (`JOBHUNTER_STACK_PREBUILT`): the dev stack test's start
(tests/stack/test_dev_stack.py) and the compose test stack's `up` (tests/support/prodstack.py); F6 fix
rounds 1 and 2, Rulings F6-2 and F6-4.

`JOBHUNTER_STACK_PREBUILT` names compose services, comma-separated (e.g. `embedder`), whose existing local
image a stack run uses as it is and never builds: for a host that can reach PyPI but not every build source of
an image (the real embedder's Debian mirror and model download). Compose builds the missing image of a service
that has a `build:` section on `up` (unless `--no-build`) and on `run` (which has no such flag), so a missing
prebuilt image would be built after all. So a start first checks that the image each prebuilt service uses
exists locally (`require_prebuilt`: the image the project's own compose model names, read with
`--profile '*' config --format json`, never a tag known here), and fails naming the service and the image when
one does not, and naming a prebuilt name that is no service of the model, before anything is built or started.
Then it builds only the services whose image is no prebuilt one (`to_build`): services that share a tag build
the same image (migrate, api and worker share the app image), so building one of them would replace a
prebuilt image. The stack then starts with `up --no-build`, and a later `run` meets only images that exist.
Without prebuilt services every image is built (`up -d --build --wait`), as always.
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


def image_of(model: Mapping[str, Any], service: str) -> str:
    """The image `service` of the compose `model` uses: its `image`, or compose's own name for its build,
    `<project>-<service>`."""
    image: str = model["services"][service].get("image") or f"{model['name']}-{service}"
    return image


def images_of(model: Mapping[str, Any], services: Sequence[str]) -> dict[str, str]:
    """The image each of `services` uses in the compose `model` (`config --format json`), by service. A name
    that is no service of the model fails (`pytest.fail`) naming it: it would protect nothing."""
    found = model.get("services") or {}
    if unknown := [name for name in services if name not in found]:
        named = ", ".join(f"`{name}`" for name in unknown)
        listed = ", ".join(f"`{name}`" for name in sorted(found))
        pytest.fail(
            f"JOBHUNTER_STACK_PREBUILT names {named}, which is no service of this stack "
            f"(its services: {listed})"
        )
    return {name: image_of(model, name) for name in services}


def to_build(model: Mapping[str, Any], built_services: Sequence[str], prebuilt: Sequence[str]) -> list[str]:
    """`built_services` without every service whose image is a prebuilt service's: a service that shares a
    prebuilt image's tag would rebuild it. A service the model does not have is left to compose."""
    protected = set(images_of(model, prebuilt).values())
    found = model.get("services") or {}
    return [name for name in built_services if name not in found or image_of(model, name) not in protected]


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


def require_prebuilt(argv: Sequence[str], prebuilt: Sequence[str]) -> dict[str, Any]:
    """Fails (`pytest.fail`) unless each prebuilt name is a service of the project `argv` (every profile's
    included) and its image exists locally; the project's compose model, for `to_build`."""
    config = subprocess.run(
        [*argv, "--profile", "*", "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=CALL_TIMEOUT_S,
    )
    if config.returncode != 0:
        pytest.fail(f"docker compose config failed ({config.returncode}):\n{config.stderr}")
    model: dict[str, Any] = json.loads(config.stdout)
    images = images_of(model, prebuilt)
    missing = {service: image for service, image in images.items() if not image_exists(image)}
    if missing:
        pytest.fail(missing_message(missing))
    return model


def start(
    argv: Sequence[str], built_services: Sequence[str], prebuilt: Sequence[str], *, timeout_s: float
) -> subprocess.CompletedProcess[bytes]:
    """The stack `argv` started (`up -d --wait`), its images built first but never a prebuilt one (module
    docstring); the `up`'s result, for the caller to judge."""
    if not prebuilt:
        return subprocess.run([*argv, "up", "-d", "--build", "--wait"], check=False, timeout=timeout_s)
    model = require_prebuilt(argv, prebuilt)
    if built := to_build(model, built_services, prebuilt):
        subprocess.run([*argv, "build", *built], check=True, timeout=timeout_s)
    return subprocess.run([*argv, "up", "-d", "--wait", "--no-build"], check=False, timeout=timeout_s)
