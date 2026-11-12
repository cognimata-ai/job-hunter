"""The CI workflows of the production images and stacks (plan M5 Task 29; R11, Deviation 40, Rulings T29-1
and T29-2), read from their YAML, with the shell of the steps that decide something run against a fake
`docker` and `sleep` on PATH:

- `ci.yml`'s `images` job, on push and pull request as the other jobs: builds the app (`runtime`), backup
  (`backup`) and docparse images and the embedder's `fake` target from the digests of `deploy/images.lock`,
  the root Dockerfile's with the optional `build_ca` secret as compose's `x-app-build` passes it, and pushes
  nothing; then scans the app, backup and docparse images with the Trivy of the lock, failing on a CRITICAL
  that has a fix.
- `prodstack.yml`, on pull requests to main and by hand: the compose test stack's modules (N1 with the outside
  reach), the preceding release's image and the drills, then every project removed with its volumes and
  `jobhunter-app:prev` removed, in every case.
- `embedder-image.yml`, on a pull request that changes `embedder/` and by hand: the real embedder image built
  and started once, passing only once its health says the model matches `embedder/model.lock`.
- `release.yml`, by hand only: the four images pushed to GHCR, `deploy/release.lock` written from their
  digests and the digest PR opened.

Every action is pinned to a full commit SHA with its tag beside it; no secret but the GITHUB_TOKEN."""

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

REPO: Final = Path(__file__).resolve().parents[2]
WORKFLOWS: Final = REPO / ".github/workflows"
CI: Final = WORKFLOWS / "ci.yml"
PRODSTACK: Final = WORKFLOWS / "prodstack.yml"
EMBEDDER: Final = WORKFLOWS / "embedder-image.yml"
RELEASE: Final = WORKFLOWS / "release.yml"
LOCK: Final = REPO / "deploy/images.lock"
_USES: Final = re.compile(r"uses: ([\w.-]+/[\w.-]+)@(\S+)( # \S+)?")
_PINNED: Final = re.compile(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}")
_SHA_AND_TAG: Final = re.compile(r"[0-9a-f]{40} # v\d+(\.\d+)*")
_DIGEST: Final = re.compile(r"[a-z0-9./_-]+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}")
# Ruling T29-1: each the newest release at least two weeks old, read with `git ls-remote` (lightweight tags,
# so the SHA is the commit's).
DOCKER_ACTIONS: Final = {
    "docker/setup-buildx-action": "f87e5991a6d7451dcb8d9637bfbc97413f497069 # v4.4.1",
    "docker/login-action": "81c6f93d4bc991e510b342a00b419b3bb4efc5c6 # v4.7.0",
    "docker/build-push-action": "c3c9e263c25d99ce0380d002d59b67737d91b0dc # v7.4.0",
}
# The build arguments of each Dockerfile and the deploy/images.lock line each one comes from.
ROOT_BASES: Final = {
    "PYTHON_IMAGE": "python",
    "UV_IMAGE": "uv",
    "NODE_IMAGE": "node",
    "RESTIC_IMAGE": "restic",
    "PGVECTOR_IMAGE": "pgvector",
}
PYTHON_BASES: Final = {"PYTHON_IMAGE": "python", "UV_IMAGE": "uv"}  # docparse/ and embedder/Dockerfile
# compose's `build_ca: { file: "${SSL_CERT_FILE:-/dev/null}" }`, as `docker build` takes it
BUILD_CA: Final = "id=build_ca,src=${SSL_CERT_FILE:-/dev/null}"
# release.yml's tag of each image: the commit it was built from
GHCR_TAG: Final = "ghcr.io/${{{{ github.repository_owner }}}}/jobhunter-{kind}:${{{{ github.sha }}}}"
# The Global Constraints' command lines (`e2e.yml` defines `DC` the same way).
PT: Final = (
    "docker compose -p jobhunter-prodtest --env-file deploy/images.lock --env-file deploy/compose.test.env "
    "-f deploy/compose.yaml -f deploy/compose.test.yaml"
)
PB: Final = PT.replace("-p jobhunter-prodtest ", "-p jobhunter-prodtest-b ")
S3: Final = "docker compose -p jobhunter-s3stub --env-file deploy/images.lock -f deploy/compose.s3stub.yaml"
STACK_MODULES: Final = (
    "tests/stack/test_backup_image.py",
    "tests/stack/test_prod_stack.py",
    "tests/stack/test_prod_network.py",
    "tests/stack/test_prod_backup.py",
    "tests/stack/test_prod_restore.py",
)
MODULES_RUN: Final = (
    "JOBHUNTER_N1_OUTSIDE=1 JOBHUNTER_STACK_PREBUILT=embedder uv run pytest -m stack -q "
    + " ".join(STACK_MODULES)
)
DRILLS_RUN: Final = (
    "JOBHUNTER_STACK_PREBUILT=embedder uv run pytest -m stack -q tests/stack/test_prod_drills.py"
)
# A `docker` that records its arguments and answers from FAKE_* variables (the container's state, its health
# from the FAKE_HEALTHY_AFTER-th `exec` on, `image inspect`'s status), and a `sleep` that returns at once.
FAKE_DOCKER: Final = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DIR/calls"
case "$1" in
  inspect)
    case "$3" in
      *ExitCode*) echo 78 ;;
      *) echo "$FAKE_RUNNING" ;;
    esac ;;
  exec)
    n=$(( $(cat "$FAKE_DIR/execs" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$FAKE_DIR/execs"
    [ "$FAKE_HEALTHY_AFTER" -gt 0 ] && [ "$n" -ge "$FAKE_HEALTHY_AFTER" ] ;;
  image)
    if [ "$2" = inspect ]; then exit "$FAKE_IMAGE_STATUS"; fi ;;
esac
"""


@dataclass(frozen=True)
class Build:
    """One `docker build` command line of a `run` step."""

    file: str
    target: str | None
    args: dict[str, str]
    secrets: list[str]
    tag: str
    context: str
    argv: list[str]


def load(path: Path) -> dict[Any, Any]:
    doc: dict[Any, Any] = yaml.safe_load(path.read_text("utf-8"))
    return doc


def triggers(doc: dict[Any, Any]) -> Any:
    return doc["on"] if "on" in doc else doc[True]  # YAML 1.1 reads a bare `on` as true


def only_job(doc: dict[Any, Any]) -> dict[str, Any]:
    (job,) = doc["jobs"].values()
    found: dict[str, Any] = job
    return found


def steps_of(job: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = job["steps"]
    return found


def all_steps(path: Path) -> list[dict[str, Any]]:
    return [step for job in load(path)["jobs"].values() for step in steps_of(job)]


def command(step: dict[str, Any]) -> str:
    """A step's `run` with its whitespace collapsed (a folded scalar keeps a trailing newline)."""
    return " ".join(str(step.get("run", "")).split())


def index_of(found: list[dict[str, Any]], run: str) -> int:
    """The index of the one step whose `run` is `run`."""
    (index,) = [i for i, step in enumerate(found) if command(step) == run]
    return index


def action(step: dict[str, Any]) -> str:
    return str(step.get("uses", "")).partition("@")[0]


def from_lock(name: str) -> str:
    """How a step reads line `name`'s reference from deploy/images.lock (as ci.yml's gitleaks step does)."""
    return f"$(grep ^{name}= deploy/images.lock | cut -d= -f2)"


def evaluated(expression: str) -> str:
    """What bash makes of `expression` in the repository root, a step's working directory."""
    done = subprocess.run(
        ["bash", "-c", f'printf %s "{expression}"'], cwd=REPO, capture_output=True, text=True, check=True
    )
    return done.stdout


def lock_lines() -> dict[str, str]:
    return dict(line.split("=", 1) for line in LOCK.read_text("utf-8").splitlines())


def values(argv: list[str], *flags: str) -> list[str]:
    """The value after each occurrence of any of `flags`."""
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg in flags]


def docker_builds(job: dict[str, Any]) -> list[Build]:
    builds = []
    for step in steps_of(job):
        argv = shlex.split(str(step.get("run", "")))
        if argv[:2] != ["docker", "build"]:
            continue
        (tag,) = values(argv, "-t", "--tag")
        (file,) = values(argv, "-f", "--file")
        targets = values(argv, "--target")
        assert len(targets) <= 1, argv
        args = dict(value.split("=", 1) for value in values(argv, "--build-arg"))
        builds.append(
            Build(file, targets[0] if targets else None, args, values(argv, "--secret"), tag, argv[-1], argv)
        )
    return builds


def pins(path: Path) -> dict[str, str]:
    """Each action's `<sha> <comment>` in a workflow file (one pin per action)."""
    found: dict[str, set[str]] = {}
    for name, ref, comment in _USES.findall(path.read_text("utf-8")):
        found.setdefault(name, set()).add(f"{ref}{comment}")
    assert all(len(refs) == 1 for refs in found.values()), found
    return {name: refs.pop() for name, refs in found.items()}


def run_script(script: str, tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """`script` under `bash -e` (a `run` step's shell) in `tmp_path`, with the fake `docker` and `sleep` first
    on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("docker", FAKE_DOCKER), ("sleep", "#!/bin/sh\nexit 0\n")):
        (bin_dir / name).write_text(body, "utf-8")
        (bin_dir / name).chmod(0o755)
    full_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_DIR": str(tmp_path),
        "FAKE_RUNNING": "true",
        "FAKE_HEALTHY_AFTER": "0",
        "FAKE_IMAGE_STATUS": "0",
        **env,
    }
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, "-e", "-c", script],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def docker_calls(tmp_path: Path) -> list[str]:
    calls = tmp_path / "calls"
    return calls.read_text("utf-8").splitlines() if calls.exists() else []


def test_images_builds_four_images_and_pushes_nothing() -> None:
    doc = load(CI)
    assert set(triggers(doc)) == {"push", "pull_request"}  # as the other jobs
    assert doc["permissions"] == {"contents": "read"}
    job = doc["jobs"]["images"]
    assert job["runs-on"] == "ubuntu-24.04" and not set(job) & {"needs", "if", "permissions"}
    expected = {
        "jobhunter-app:ci": ("Dockerfile", "runtime", ROOT_BASES),
        "jobhunter-backup:ci": ("Dockerfile", "backup", ROOT_BASES),
        "jobhunter-docparse:ci": ("docparse/Dockerfile", None, PYTHON_BASES),
        "jobhunter-embedder:ci-fake": ("embedder/Dockerfile", "fake", PYTHON_BASES),
    }
    builds = docker_builds(job)
    assert [build.tag for build in builds] == list(expected)
    # and nowhere else in ci.yml: the docparse job no longer builds its image a second time
    assert [build.tag for each in doc["jobs"].values() for build in docker_builds(each)] == list(expected)
    for build in builds:
        file, target, bases = expected[build.tag]
        assert (build.file, build.target, build.context) == (file, target, "."), build.argv
        assert build.args == {arg: from_lock(name) for arg, name in bases.items()}, build.argv
        assert build.secrets == [BUILD_CA], build.argv
        assert not {"--push", "--output", "-o"} & set(build.argv), build.argv
    # each `grep` reads exactly its own line of the lock
    lock = lock_lines()
    for name in ROOT_BASES.values():
        assert evaluated(from_lock(name)) == lock[name], name
    # no registry: nothing logged in to or pushed, and no action but the checkout
    text = yaml.safe_dump(job)
    assert not re.search(r"docker (push|login)|--push|buildx", text)
    assert [action(step) for step in steps_of(job) if "uses" in step] == ["actions/checkout"]


def test_the_scan_fails_on_a_fixable_critical() -> None:
    lock = lock_lines()
    assert re.fullmatch(r"aquasec/trivy:0\.76\.1@sha256:[0-9a-f]{64}", lock["trivy"])  # Ruling T29-1
    assert evaluated(from_lock("trivy")) == lock["trivy"]
    job = load(CI)["jobs"]["images"]
    found = steps_of(job)
    scans = [i for i, step in enumerate(found) if "trivy" in str(step.get("run", ""))]
    scanned = ["jobhunter-app:ci", "jobhunter-backup:ci", "jobhunter-docparse:ci"]
    assert [shlex.split(str(found[i]["run"])) for i in scans] == [
        [
            "docker",
            "run",
            "--rm",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            from_lock("trivy"),
            "image",
            "--scanners",
            "vuln",  # the gate is a fixable CRITICAL vulnerability; the repository's secrets are gitleaks'
            "--exit-code",
            "1",
            "--severity",
            "CRITICAL",
            "--ignore-unfixed",
            tag,
        ]
        for tag in scanned
    ]
    # each scanned image is one the job built, before the first scan
    built = [i for i, step in enumerate(found) if command(step).startswith("docker build")]
    assert max(built) < min(scans)
    assert set(scanned) <= {build.tag for build in docker_builds(job)}
    # a finding fails the job: no scan may fail quietly or be skipped
    for i in scans:
        assert not set(found[i]) & {"continue-on-error", "if"}, found[i]
        assert "||" not in str(found[i]["run"]) and ";" not in str(found[i]["run"])
    assert "continue-on-error" not in job


def test_no_build_context_holds_a_secret_or_a_raw_recording() -> None:
    """Every image is built from the repository root (`context: .`), and .dockerignore keeps `.secrets` (the
    secret files) and `.fixtures-raw` (raw recordings with personal data) out of it, with no later `!` line
    letting either back in; the scan checks vulnerabilities only, so nothing else would notice."""
    lines = [line.strip() for line in (REPO / ".dockerignore").read_text("utf-8").splitlines()]
    patterns = [line for line in lines if line and not line.startswith("#")]
    for name in (".secrets", ".fixtures-raw"):
        assert name in patterns, name
        assert not [p for p in patterns if p.startswith("!") and p.lstrip("!/").startswith(name)], name


def test_prodstack_triggers_are_pull_requests_to_main_and_dispatch() -> None:
    doc = load(PRODSTACK)
    assert triggers(doc) == {"pull_request": {"branches": ["main"]}, "workflow_dispatch": {}}
    assert doc["permissions"] == {"contents": "read"}
    assert list(doc["jobs"]) == ["prodstack"]
    job = doc["jobs"]["prodstack"]
    assert (job["runs-on"], job["timeout-minutes"]) == ("ubuntu-24.04", 90)
    assert {name: " ".join(value.split()) for name, value in job["env"].items()} == {
        "PT": PT,
        "PB": PB,
        "S3": S3,
    }


def test_prodstack_runs_the_stack_modules_then_the_drills() -> None:
    found = steps_of(load(PRODSTACK)["jobs"]["prodstack"])
    ci = steps_of(load(CI)["jobs"]["unit"])
    # the checkout as ci.yml pins it, with the whole history: build-prev-image.sh reads the parent of the
    # commit that added 0011, which a shallow clone lacks
    assert found[0]["uses"] == ci[0]["uses"]
    assert found[0]["with"] == {"fetch-depth": 0, "persist-credentials": False}
    assert (found[1]["uses"], found[1]["with"]) == (ci[1]["uses"], ci[1]["with"])
    order = [
        index_of(found, run)
        for run in (
            "uv sync --frozen",
            "sudo scripts/test-secrets.sh",
            "$PT build embedder",  # the fake target, which JOBHUNTER_STACK_PREBUILT leaves unbuilt
            MODULES_RUN,
            "scripts/build-prev-image.sh",
            DRILLS_RUN,
        )
    ]
    assert order == sorted(order) and order[0] == 2
    # the drills as a whole module (both fixtures are function-scoped), after the image they need
    assert "-k" not in DRILLS_RUN.split()
    for path in (*STACK_MODULES, "tests/stack/test_prod_drills.py", "scripts/build-prev-image.sh"):
        assert (REPO / path).is_file(), path
    # nothing before the tear-down runs on a condition
    assert not any("if" in step for step in found[: order[-1] + 1])


def test_prodstack_always_tears_down_every_project(tmp_path: Path) -> None:
    job = load(PRODSTACK)["jobs"]["prodstack"]
    found = steps_of(job)
    tail = found[-4:]
    assert [command(step) for step in tail[:3]] == ["$PT down -v", "$PB down -v", "$S3 down -v"]
    assert [step for step in found if "if" in step] == tail
    assert all(step["if"] == "always()" for step in tail)  # after a failure or a cancel too
    assert index_of(found, DRILLS_RUN) < found.index(tail[0])
    # each expands to its project's command line (one step each, so one failing leaves the next to run)
    env = {name: str(value) for name, value in job["env"].items()}
    for step, line in zip(tail[:3], (PT, PB, S3), strict=True):
        (tmp_path / "calls").unlink(missing_ok=True)
        assert run_script(str(step["run"]), tmp_path, env).returncode == 0
        assert docker_calls(tmp_path) == [f"{line.removeprefix('docker ')} down -v"]
    # and Task 27's image goes last, when it is there; its absence (an earlier failure) is not a second one
    remove = str(tail[3]["run"])
    for status, calls in (
        ("0", ["image inspect jobhunter-app:prev", "image rm jobhunter-app:prev"]),
        ("1", ["image inspect jobhunter-app:prev"]),
    ):
        (tmp_path / "calls").unlink(missing_ok=True)
        assert run_script(remove, tmp_path, {**env, "FAKE_IMAGE_STATUS": status}).returncode == 0
        assert docker_calls(tmp_path) == calls


def test_release_is_dispatch_only_and_writes_release_lock(tmp_path: Path) -> None:
    doc = load(RELEASE)
    assert triggers(doc) == {"workflow_dispatch": {}}
    assert doc["permissions"] == {"contents": "write", "packages": "write", "pull-requests": "write"}
    found = steps_of(only_job(doc))
    names = [action(step) for step in found if "uses" in step]
    assert names == ["actions/checkout", *DOCKER_ACTIONS, *["docker/build-push-action"] * 3]
    login = next(step for step in found if action(step) == "docker/login-action")
    assert login["with"] == {
        "registry": "ghcr.io",
        "username": "${{ github.actor }}",
        "password": "${{ secrets.GITHUB_TOKEN }}",
    }
    # the base images and the CA bundle file, as outputs of the `lock` step
    (lock_step,) = [step for step in found if step.get("id") == "lock"]
    (tmp_path / "deploy").mkdir()
    shutil.copy(LOCK, tmp_path / "deploy/images.lock")
    output = tmp_path / "output"
    env = {"GITHUB_OUTPUT": str(output)}
    assert run_script(str(lock_step["run"]), tmp_path, env).returncode == 0
    lock = lock_lines()
    assert dict(line.split("=", 1) for line in output.read_text("utf-8").splitlines()) == {
        **{name: lock[name] for name in ROOT_BASES.values()},
        "build_ca": "/dev/null",  # SSL_CERT_FILE unset, as compose's `${SSL_CERT_FILE:-/dev/null}`
    }
    # the four images: the root Dockerfile's two targets, docparse and the real embedder, each pushed
    builds = {step["id"]: step["with"] for step in found if action(step) == "docker/build-push-action"}
    expected = {
        "app": ("Dockerfile", "runtime", ROOT_BASES),
        "backup": ("Dockerfile", "backup", ROOT_BASES),
        "docparse": ("docparse/Dockerfile", None, PYTHON_BASES),
        "embedder": ("embedder/Dockerfile", "runtime", PYTHON_BASES),
    }
    assert list(builds) == list(expected)
    for kind, given in builds.items():
        file, target, bases = expected[kind]
        assert (given["context"], given["file"], given.get("target")) == (".", file, target), kind
        assert dict(line.split("=", 1) for line in given["build-args"].splitlines()) == {
            arg: f"${{{{ steps.lock.outputs.{name} }}}}" for arg, name in bases.items()
        }, kind
        assert given["secret-files"] == "build_ca=${{ steps.lock.outputs.build_ca }}", kind
        assert given["push"] is True, kind
        assert given["tags"] == GHCR_TAG.format(kind=kind), kind
    # deploy/release.lock from the four digests, one line per variable compose.yaml reads from it
    (write,) = [step for step in found if "> deploy/release.lock" in str(step.get("run", ""))]
    assert write["env"] == {
        "OWNER": "${{ github.repository_owner }}",
        **{f"{kind.upper()}_DIGEST": f"${{{{ steps.{kind}.outputs.digest }}}}" for kind in expected},
    }
    digests = {kind: f"sha256:{str(n) * 64}" for n, kind in enumerate(expected, start=1)}
    env = {"OWNER": "owner", "GITHUB_SHA": "f" * 40, **{f"{k.upper()}_DIGEST": d for k, d in digests.items()}}
    (tmp_path / "deploy/release.lock").write_text("app_image=stale\n", "utf-8")
    assert run_script(str(write["run"]), tmp_path, env).returncode == 0
    lines = (tmp_path / "deploy/release.lock").read_text("utf-8").splitlines()
    written = [line for line in lines if not line.startswith("#")]
    assert written == [f"{kind}_image=ghcr.io/owner/jobhunter-{kind}@{digests[kind]}" for kind in expected]
    assert all(_DIGEST.fullmatch(line.split("=", 1)[1]) for line in written)
    compose = (REPO / "deploy/compose.yaml").read_text("utf-8")
    assert set(re.findall(r"\$\{(\w+_image):\?\}", compose)) == {line.split("=", 1)[0] for line in written}
    # then the digest PR, last, with the job's token
    pr = found[-1]
    assert found.index(write) == len(found) - 2
    assert pr["env"] == {"GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    script = str(pr["run"])
    assert "git add deploy/release.lock" in script and "gh pr create" in script
    assert "git push" in script and "--force" not in script


def test_the_embedder_build_runs_only_on_embedder_changes_or_dispatch(tmp_path: Path) -> None:
    doc = load(EMBEDDER)
    assert triggers(doc) == {"pull_request": {"paths": ["embedder/**"]}, "workflow_dispatch": {}}
    assert doc["permissions"] == {"contents": "read"}
    job = only_job(doc)
    found = steps_of(job)
    # the real image (the model download, checked against model.lock at build): its last stage, not `fake`
    (build,) = docker_builds(job)
    assert (build.file, build.target, build.context) == ("embedder/Dockerfile", "runtime", ".")
    assert build.args == {arg: from_lock(name) for arg, name in PYTHON_BASES.items()}
    assert build.secrets == [BUILD_CA]
    assert "EMBEDDER_FAKE" not in EMBEDDER.read_text("utf-8")
    # started once, as compose.yaml runs it, with no network: the model is in the image
    (start,) = [step for step in found if command(step).startswith("docker run")]
    argv = shlex.split(str(start["run"]))
    assert argv[:3] == ["docker", "run", "-d"] and argv[-1] == build.tag
    (name,) = values(argv, "--name")
    assert values(argv, "--network") == ["none"] and "--read-only" in argv
    assert values(argv, "--user") == ["10004:10004"]
    # passes once `embedder-health` does (a 200 with checksum_ok: true), fails when the embedder exits first
    # (exit 78 on a checksum mismatch) or is not healthy in time
    (wait,) = [step for step in found if "embedder-health" in str(step.get("run", ""))]
    assert found.index(start) < found.index(wait)
    health = f"exec {name} embedder-health"

    def wait_for(case: str, **env: str) -> tuple[int, str, list[str]]:
        (tmp_path / case).mkdir()
        done = run_script(str(wait["run"]), tmp_path / case, env)
        return done.returncode, done.stderr, docker_calls(tmp_path / case)

    status, _, calls = wait_for("healthy", FAKE_HEALTHY_AFTER="3")
    assert status == 0 and calls.count(health) == 3
    status, stderr, calls = wait_for("exited", FAKE_RUNNING="false", FAKE_HEALTHY_AFTER="1")
    assert status != 0 and "78" in stderr
    assert f"logs {name}" in calls and health not in calls
    status, _, calls = wait_for("never", FAKE_HEALTHY_AFTER="0")
    assert status != 0 and f"logs {name}" in calls
    assert 0 < calls.count(health) <= 300  # bounded


def test_every_action_is_pinned_to_a_sha() -> None:
    for path in (CI, PRODSTACK, EMBEDDER, RELEASE):
        used = [str(step["uses"]) for step in all_steps(path) if "uses" in step]
        assert used and all(_PINNED.fullmatch(name) for name in used), (path, used)
        assert all(_SHA_AND_TAG.fullmatch(pin) for pin in pins(path).values()), path  # the tag beside it
    ci = pins(CI)
    checkout, setup_uv = ci["actions/checkout"], ci["astral-sh/setup-uv"]
    assert [str(step["uses"]) for step in steps_of(load(CI)["jobs"]["images"]) if "uses" in step] == [
        f"actions/checkout@{checkout.split()[0]}"
    ]
    assert pins(PRODSTACK) == {"actions/checkout": checkout, "astral-sh/setup-uv": setup_uv}
    assert pins(EMBEDDER) == {"actions/checkout": checkout}
    assert pins(RELEASE) == {"actions/checkout": checkout, **DOCKER_ACTIONS}  # Ruling T29-1


def test_no_secret_but_the_github_token_is_referenced() -> None:
    for path in (CI, PRODSTACK, EMBEDDER):
        text = path.read_text("utf-8")
        assert not re.search(r"\$\{\{[^}]*\b(secrets|github\.token)\b", text), path
    release = RELEASE.read_text("utf-8")
    expressions = re.findall(r"\$\{\{([^}]*)\}\}", release)
    assert [e.strip() for e in expressions if "secrets" in e] == ["secrets.GITHUB_TOKEN"] * 2  # login, the PR
    assert "github.token" not in release
    # the only build secret is the optional CA bundle
    for path in (CI, EMBEDDER):
        for job in load(path)["jobs"].values():
            for build in docker_builds(job):
                assert set(build.secrets) <= {BUILD_CA}, build.argv
    for step in all_steps(RELEASE):
        if action(step) == "docker/build-push-action":
            assert not {"secrets", "secret-envs", "github-token"} & set(step["with"])
            assert str(step["with"]["secret-files"]).startswith("build_ca=")
    # no checkout keeps the token
    for path in (CI, PRODSTACK, EMBEDDER, RELEASE):
        for step in all_steps(path):
            if action(step) == "actions/checkout":
                assert step["with"]["persist-credentials"] is False, path
    # and no environment of the jobs this task writes holds one
    assert set(load(PRODSTACK)["jobs"]["prodstack"]["env"]) == {"PT", "PB", "S3"}
    for job in (load(CI)["jobs"]["images"], only_job(load(EMBEDDER)), only_job(load(RELEASE))):
        assert "env" not in job
