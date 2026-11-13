"""The backup image (plan M5 Task 10, Deviations 28-30, R18): the `Dockerfile`'s `backup` stage on the app's
runtime, with restic 0.19.1 from its pinned image and the PostgreSQL 17 client from the pinned pgvector image;
the app image still the `runtime` stage; the volumes' mount points; and the dev stack's on-demand `shipper`
shipping one manifest into a local repository.

Run explicitly with `-m stack`, after `$DC down`. The module builds `jobhunter-app:dev` (`$DC build api`) and
`jobhunter-backup:dev` (`$DC build shipper`) through `deploy/compose.dev.yaml`, as the dev and E2E stacks
build them; it removes no image (Ruling T10-1). The shipping test runs in the dev project, `jobhunter-dev`, as
`$DC run --rm shipper` does: it removes that project's `restic_repo` and `backup_staging` volumes first, so
the repository starts empty, and at its end runs `$DC down` (never `-v`) and removes those two volumes and any
other volume of the project it created; every other volume of `jobhunter-dev`, its database among them,
stays. The volume test removes the volumes it creates. The module is skipped while a stack runs: the dev stack
itself, or another project on the dev stack's subnet.
"""

import json
import os
import re
import secrets
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from tests.support.manifest_file import WRITE_MANIFEST, written_manifest

pytestmark = pytest.mark.stack
REPO: Final = Path(__file__).resolve().parents[2]
DEV_PROJECT: Final = "jobhunter-dev"  # `name:` of compose.dev.yaml
# The projects on the dev stack's subnet (172.30.0.0/24): only one runs at a time (M5 Global Constraints).
OTHER_PROJECTS: Final = ("jobhunter-stacktest", "jobhunter-prodtest", "jobhunter-prodtest-b")
DC: Final = [
    "docker",
    "compose",
    "--env-file",
    str(REPO / "deploy/images.lock"),
    *(
        arg
        for name in ("compose.dev.yaml", "compose.fixtures.yaml", "compose.fakellm.yaml", "compose.e2e.yaml")
        for arg in ("-f", str(REPO / "deploy" / name))
    ),
]
APP_IMAGE: Final = "jobhunter-app:dev"
BACKUP_IMAGE: Final = "jobhunter-backup:dev"
# Removed first, so the shipper's repository and staging start empty, and again at the end.
FRESH_VOLUMES: Final = (f"{DEV_PROJECT}_restic_repo", f"{DEV_PROJECT}_backup_staging")
PGCLIENT: Final = ("pg_dump", "pg_restore", "psql")
PGCLIENT_LIBRARIES: Final = (
    "libpq.so.5",
    "libgssapi_krb5.so.2",
    "libldap-2.5.so.0",
    "libkrb5.so.3",
    "libk5crypto.so.3",
    "libkrb5support.so.0",
    "liblber-2.5.so.0",
    "libsasl2.so.2",
    "libkeyutils.so.1",
)
CALL_TIMEOUT_S: Final = 180
BUILD_TIMEOUT_S: Final = 1800
KEY_VARIABLE: Final = "JOBHUNTER_ANTHROPIC_API_KEY"


def run(*argv: str, timeout: float = CALL_TIMEOUT_S, check: bool = True) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    if check and done.returncode != 0:
        pytest.fail(f"{' '.join(argv[:6])} … exited {done.returncode}:\n{done.stdout}\n{done.stderr}")
    return done


def lock(name: str) -> str:
    for line in (REPO / "deploy/images.lock").read_text(encoding="utf-8").splitlines():
        key, _, reference = line.partition("=")
        if key == name:
            return reference
    raise LookupError(f"deploy/images.lock has no {name} line")


def last_json(stdout: str) -> object:
    """The JSON line a `jobhunter` command prints last on stdout."""
    return json.loads([line for line in stdout.splitlines() if line.strip()][-1])


def _project_in_use(project: str, *, networks: bool) -> str | None:
    kinds = [("running containers", ["docker", "ps", "--quiet"])]
    if networks:
        kinds.append(("networks", ["docker", "network", "ls", "--quiet"]))
    found = []
    for kind, command in kinds:
        listed = run(*command, "--filter", f"label=com.docker.compose.project={project}")
        if listed.stdout.split():
            found.append(f"{kind} ({len(listed.stdout.split())})")
    return ", ".join(found) or None


def _project_volumes() -> set[str]:
    listed = run(
        "docker", "volume", "ls", "--quiet", "--filter", f"label=com.docker.compose.project={DEV_PROJECT}"
    )
    return set(listed.stdout.split())


def _remove_volumes(names: set[str]) -> None:
    for name in sorted(names):
        run("docker", "volume", "rm", "--force", name)


@pytest.fixture(scope="module")
def images() -> None:
    """Both images, built through compose.dev.yaml; the module is skipped while a stack runs."""
    for project, networks in ((DEV_PROJECT, False), *((name, True) for name in OTHER_PROJECTS)):
        in_use = _project_in_use(project, networks=networks)
        if in_use is not None:
            pytest.skip(
                f"compose project {project} has {in_use}: this module runs the dev project's shipper and "
                "stops it at its end. It never stops another stack; bring it down yourself (`$DC down`, "
                "keeping its volumes) to run the backup image test."
            )
    # restic_password and the role passwords: kept when they exist, never printed. Without a key variable the
    # script writes an empty anthropic_key when it has none, and keeps the one there. As root, through
    # `sudo -n` (which fails rather than ask), as prodstack.ensure_test_secrets runs test-secrets.sh: the
    # script sets group 10010 on its directory at every run, which a user who is not root (a CI runner)
    # cannot.
    env = {name: value for name, value in os.environ.items() if name != KEY_VARIABLE}
    subprocess.run(
        ["sudo", "-n", str(REPO / "scripts/dev-secrets.sh")],
        check=True,
        capture_output=True,
        timeout=CALL_TIMEOUT_S,
        env=env,
    )
    run(*DC, "build", "api", "shipper", timeout=BUILD_TIMEOUT_S)


@pytest.fixture(scope="module")
def dev_project(images: None) -> Iterator[set[str]]:
    """The dev project with fresh `restic_repo` and `backup_staging` volumes (module docstring); yields the
    project's volumes that were there before, which it keeps."""
    _remove_volumes(set(FRESH_VOLUMES) & _project_volumes())
    kept = _project_volumes()
    try:
        yield kept
    finally:
        down = run(*DC, "down", "--remove-orphans", check=False, timeout=300)
        _remove_volumes(_project_volumes() - kept)
    assert down.returncode == 0, down.stderr


@pytest.fixture
def fresh_volumes() -> Iterator[list[str]]:
    """Names for new named volumes; whatever of them exists at the end is removed."""
    names = [f"jh-backup-image-test-{secrets.token_hex(4)}-{n}" for n in range(5)]
    try:
        yield names
    finally:
        existing = set(run("docker", "volume", "ls", "--quiet").stdout.split())
        _remove_volumes(set(names) & existing)


def in_image(image: str, *command: str) -> str:
    return run("docker", "run", "--rm", "--network", "none", image, *command).stdout


def test_restic_is_0_19_1(images: None) -> None:
    assert in_image(BACKUP_IMAGE, "/usr/local/bin/restic", "version").startswith(
        "restic 0.19.1 compiled with"
    )
    assert in_image(BACKUP_IMAGE, "sh", "-c", "command -v restic").strip() == "/usr/local/bin/restic"


def test_the_app_image_is_still_the_runtime_stage(images: None) -> None:
    probe = "import os; print(os.path.exists('/usr/local/bin/restic'), os.path.exists('/opt/pgclient'))"
    assert in_image(APP_IMAGE, "python", "-c", probe).split() == ["False", "False"]

    def layers(image: str) -> list[str]:
        return list(
            json.loads(run("docker", "image", "inspect", "--format", "{{json .RootFS.Layers}}", image).stdout)
        )

    app, backup = layers(APP_IMAGE), layers(BACKUP_IMAGE)
    assert backup[: len(app)] == app and len(backup) > len(app)  # the backup stage builds on this very image


def test_a_subfolder_restore_lands_at_the_exact_path(images: None) -> None:
    """The wrapper on the real binary: a file backed up as `/data/manifests/<uuid>.json` (by a verbatim file
    list, as the shipper names it) restores, by `snapshotID:/data/manifests`, to `<target>/<uuid>.json`."""
    script = """
import asyncio, os, pathlib, secrets, uuid
from jobhunter.backup.restic import Restic
from jobhunter.backup.runner import SubprocessRunner
from jobhunter.config import load_config

async def main():
    secrets_dir = pathlib.Path("/tmp/secrets")
    secrets_dir.mkdir(mode=0o700)
    (secrets_dir / "restic_password").write_text(secrets.token_hex(16))
    env = {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "jobhunter"}
    env["RESTIC_REPOSITORY"] = "/data/restic-repo"
    restic = Restic(load_config("backup", env, secrets_dir), SubprocessRunner())
    assert await restic.init_if_absent() is True
    assert await restic.init_if_absent() is False
    name = f"{uuid.uuid4()}.json"
    source = pathlib.Path("/data/manifests") / name
    source.write_bytes(b'{"format":1}')
    listing = pathlib.Path("/data/staging/files.list")
    listing.write_text(f"{source}\\n")
    snapshot = await restic.backup(files_from=listing, tags=["manifests"])
    [info] = await restic.snapshots(tags=["manifests"])
    assert (info.id, info.tags, info.paths) == (snapshot, ("manifests",), (str(source),)), info
    target = pathlib.Path("/tmp/target")
    await restic.restore(snapshot=snapshot, subfolder="/data/manifests", target=target)
    assert sorted(os.listdir(target)) == [name], os.listdir(target)
    assert (target / name).read_bytes() == source.read_bytes()
    print("restored", name)

asyncio.run(main())
"""
    out = in_image(BACKUP_IMAGE, "python", "-c", script)
    assert re.fullmatch(r"restored [0-9a-f-]{36}\.json\n", out), out


def test_pg_dump_is_the_server_build(images: None) -> None:
    server = in_image(lock("pgvector"), "postgres", "--version").strip()
    assert server.startswith("postgres (PostgreSQL) 17.11 ")
    for binary in PGCLIENT:
        client = in_image(BACKUP_IMAGE, f"/opt/pgclient/bin/{binary}", "--version").strip()
        assert client == server.replace(
            "postgres", binary, 1
        )  # one build: e.g. 17.11 (Debian 17.11-1.pgdg12+2)


def test_no_library_is_missing(images: None) -> None:
    linked = in_image(BACKUP_IMAGE, "ldd", *(f"/opt/pgclient/bin/{binary}" for binary in PGCLIENT))
    assert "not found" not in linked, linked
    for library in PGCLIENT_LIBRARIES:
        resolved = {line.split()[2] for line in linked.splitlines() if line.strip().startswith(f"{library} ")}
        assert resolved == {f"/opt/pgclient/lib/{library}"}, (library, resolved)


def test_it_runs_as_10001(images: None) -> None:
    for image in (BACKUP_IMAGE, APP_IMAGE):
        assert run("docker", "image", "inspect", "--format", "{{.Config.User}}", image).stdout.strip() == (
            "10001:10001"
        )
    assert in_image(BACKUP_IMAGE, "id", "-u").strip() == in_image(BACKUP_IMAGE, "id", "-g").strip() == "10001"


def test_fresh_volumes_mount_10001_0700(images: None, fresh_volumes: list[str]) -> None:
    """R18: Docker gives a new named volume the owner and mode of the image's directory it is mounted on."""
    mounts = [
        (APP_IMAGE, "/data/cv"),
        (APP_IMAGE, "/data/manifests"),
        (APP_IMAGE, "/data/exports"),
        (BACKUP_IMAGE, "/data/staging"),
        (BACKUP_IMAGE, "/data/restic-repo"),
    ]
    for volume, (image, directory) in zip(fresh_volumes, mounts, strict=True):
        run("docker", "volume", "create", volume)
        mounted = run(
            "docker", "run", "--rm", "--network", "none", "-v", f"{volume}:{directory}", image,
            "stat", "-c", "%u:%g %a", directory,
        )  # fmt: skip
        assert (directory, mounted.stdout.strip()) == (directory, "10001:10001 700")


def test_ship_into_a_local_repository(dev_project: set[str]) -> None:
    init = run(*DC, "run", "--rm", "-T", "shipper", "jobhunter", "backup", "init", timeout=600)
    assert last_json(init.stdout) == {"created": True}
    probe = "id -u; id -g; stat -c '%u:%g %a' /data/restic-repo /data/staging"
    whoami = run(*DC, "run", "--rm", "-T", "--no-deps", "shipper", "sh", "-c", probe)
    assert whoami.stdout.splitlines() == ["10001", "10001", "10001:10001 700", "10001:10001 700"]

    # One manifest file as a purge writes it (`tests.support.manifest_file`), in the one-off `migrate`, which
    # mounts the manifests volume writable; the replay of it by the `migrate` the shipper depends on deletes
    # nothing.
    writer = run(*DC, "run", "--rm", "-T", "--no-deps", "migrate", "python", "-c", WRITE_MANIFEST, "migrate")
    manifest_uuid, sha, stream = written_manifest(writer.stdout)
    assert stream[manifest_uuid] == sha
    if f"{DEV_PROJECT}_manifests" not in dev_project:
        assert list(stream) == [manifest_uuid]  # a new volume: its one manifest is this test's

    shipped = last_json(run(*DC, "run", "--rm", "-T", "shipper", timeout=600).stdout)
    assert isinstance(shipped, dict)
    assert (shipped["shipped"], shipped["files"]) == (True, len(stream))
    snapshot_id = shipped["snapshot_id"]

    listing = run(
        *DC,
        "run", "--rm", "-T", "--no-deps", "-e", "RESTIC_PASSWORD_FILE=/run/secrets/restic_password",
        "shipper", "restic", "snapshots", "--json", "--no-cache", "--no-lock",
    )  # fmt: skip
    snapshots = json.loads(listing.stdout)
    paths = sorted(f"/data/manifests/{name}.json" for name in stream)
    assert [(s["id"], s["hostname"], s["tags"], sorted(s["paths"])) for s in snapshots] == [
        (snapshot_id, "jobhunter-backup", ["manifests"], paths)  # the repository was new: one snapshot
    ]

    assert re.fullmatch(r"[0-9a-f]{64}", snapshot_id)  # so it is safe in the query's literal
    query = (
        "SELECT i.manifest_uuid, encode(i.file_sha256, 'hex') FROM manifest_shipments s "  # noqa: S608 - hex id
        "JOIN manifest_shipment_items i ON i.shipment_id = s.id WHERE s.restic_snapshot_id = "
        f"'{snapshot_id}'"
    )
    psql = [
        "psql",
        "-U",
        "jobhunter_bootstrap",
        "-d",
        "jobhunter",
        "-AtX",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        query,
    ]
    rows = run(*DC, "exec", "-T", "postgres", *psql).stdout.split()
    assert dict(row.split("|") for row in rows) == stream  # one shipment row, one item per file
    assert len(rows) == len(stream)
