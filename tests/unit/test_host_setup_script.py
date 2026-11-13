"""`scripts/host-setup.sh`, the production host's one-time setup (spec §19.8 "Host setup"; plan M5 Task 22,
Ruling T22-3).

Every case is a `--dry-run` with a temporary `--secrets-from` (Ruling T22-1 (b)): a real run's targets are
the host's /etc/jobhunter, its systemd and the `jobhunter` prod project, which no test touches. A dry run
reads the names in the source directory (never a file's content), runs the read-only preflight and prints
each command it would run, executing none. Shims stand first on PATH for every command that changes the
host, and record any call. The preflight's reading commands (`getent`, `swapon`, `findmnt`, `lsblk`) are stubs
too, which log their arguments and answer from the test, so the host's own group database and swap are never
consulted. Each source file holds a planted dummy value, which must never appear in the output.
"""

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest
from jobhunter.secret_names import SECRET_NAMES

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO / "scripts/host-setup.sh"
# deploy/secrets.example's names, in its order: the secrets each installed by its own command.
NAMES: Final = tuple(
    line.split()[0]
    for line in (REPO / "deploy/secrets.example").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.startswith("#")
)
# The two that may be empty files (deploy/secrets.example): no rotation in progress, no ntfy token.
OPTIONAL: Final = ("data_key_previous", "ntfy_token")
ROLES: Final = ("api", "worker", "migrate", "backup", "restore")
PLANTED: Final = "planted-by-the-dry-run-test"
# What changes the host in a real run; none may run in a dry run.
CHANGES: Final = ("groupadd", "id", "install", "chmod", "chown", "systemctl", "docker", "rm", "nft", "sudo")
# What the preflight reads with, in a dry run too; stubbed.
PROBES: Final = ("getent", "swapon", "findmnt", "lsblk")
SECRETS_DIR: Final = "/etc/jobhunter/secrets"
COMPOSE: Final = (
    "docker compose -p jobhunter --env-file deploy/images.lock --env-file deploy/release.lock "
    "--env-file /etc/jobhunter/jobhunter.env -f deploy/compose.yaml"
)
NOTE: Final = "host-setup: "
GETENT: Final = "getent group 10010"
PASSWD: Final = "getent passwd"
SWAPON: Final = "swapon --show=NAME --noheadings --raw"
SWAP_PASSED: Final = f"{NOTE}the swap check passed: no swap, or encrypted swap only"
NOT_ENCRYPTED: Final = (
    "host-setup: swap on {} is not encrypted: run the host without swap (swapoff, and remove it from "
    "/etc/fstab) or with encrypted swap only (§19.8); nothing was changed\n"
)
# A probe's answers: its last argument → (stdout, exit status). Unanswered, it fails with status 99.
Answers = dict[str, dict[str, tuple[str, int]]]
# Host users whose primary gid is not 10010; `service` has uid 10010 (the third field), which does not count.
OTHER_USERS: Final = (
    "root:x:0:0:root:/root:/bin/bash\n"
    "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
    "service:x:10010:100::/home/service:/bin/sh\n"
)
# By default no group 10010 (getent's status 2: no such entry), no host user with primary gid 10010, no swap.
DEFAULTS: Final[Answers] = {
    "getent": {"10010": ("", 2), "passwd": (OTHER_USERS, 0)},
    "swapon": {"--raw": ("", 0)},
}


@dataclass(frozen=True)
class DryRun:
    done: subprocess.CompletedProcess[str]
    source: Path
    probed: list[str]  # the probes' calls, in order, each as `<command> <arguments>`


def probe_stub(command: str, answers: dict[str, tuple[str, int]], log: Path) -> str:
    cases = "".join(
        f"{shlex.quote(last)}) printf '%s' {shlex.quote(out)}; exit {status} ;;\n"
        for last, (out, status) in answers.items()
    )
    return (
        f'#!/bin/sh\nprintf "%s\\n" "{command} $*" >> {shlex.quote(str(log))}\n'
        f"for last; do :; done\ncase $last in\n{cases}"
        f'*) echo "{command}: no answer for $last" >&2; exit 99 ;;\nesac\n'
    )


def without_nft(path: str) -> str:
    """`path` without its directories that hold an `nft`: a host without nftables."""
    return os.pathsep.join(d for d in path.split(os.pathsep) if d and not os.access(Path(d, "nft"), os.X_OK))


def dry_run(
    tmp_path: Path,
    present: tuple[str, ...],
    *flags: str,
    answers: Answers | None = None,
    nft: bool = True,
) -> DryRun:
    """The script's dry run on a new source directory holding `present`, each file a planted value, the probes
    answering from `answers` over `DEFAULTS`, answer by answer, and `nft` on PATH (a shim) unless `nft` is
    false (then no directory of PATH holds one); it fails the test when the run executed any command of
    `CHANGES`."""
    source = tmp_path / "secrets-src"
    source.mkdir()
    for name in present:
        (source / name).write_text(f"{PLANTED}-{name}\n", encoding="utf-8")
    shims = tmp_path / "bin"
    shims.mkdir()
    record, log = tmp_path / "changed", tmp_path / "probed"
    for command in CHANGES:
        if command != "nft" or nft:
            (shims / command).write_text(f'#!/bin/sh\nprintf "%s\\n" "{command} $*" >> "{record}"\nexit 97\n')
    given = {command: {**DEFAULTS.get(command, {}), **(answers or {}).get(command, {})} for command in PROBES}
    for command in PROBES:
        (shims / command).write_text(probe_stub(command, given.get(command, {}), log))
    for shim in shims.iterdir():
        shim.chmod(0o755)
    path = f"{shims}{os.pathsep}{os.environ['PATH'] if nft else without_nft(os.environ['PATH'])}"
    assert (shutil.which("nft", path=path) is None) is not nft, "nft on PATH as the case says"
    env = {"PATH": path, "HOME": str(tmp_path), "LC_ALL": "C.UTF-8"}
    done = subprocess.run(
        [str(SCRIPT), "--secrets-from", str(source), *flags, "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert not record.exists(), record.read_text()
    probed = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return DryRun(done, source, probed)


def commands(stdout: str) -> list[str]:
    """The printed commands, without the notes (`host-setup: …`) between them."""
    return [line for line in stdout.splitlines() if not line.startswith(NOTE)]


def expected(
    source: Path, *, empty: tuple[str, ...] = (), deleted: bool = False, group: bool = True
) -> list[str]:
    """§19.8's host steps in the order of plan M5 Task 22 (the swap check runs in the preflight, Ruling
    T22-3): the group (unless `group` is false: it exists), the directory, each secret (an empty file for each
    of `empty`), the egress rule's unit, each role's startup check; then, with `--delete-sources`, the sources
    that were installed."""
    sources = {name: "/dev/null" if name in empty else str(source / name) for name in NAMES}
    lines = [
        *(["groupadd --gid 10010 jobhunter-secrets"] if group else []),
        f"install -d -o root -g 10010 -m 0750 {SECRETS_DIR}",
        *(f"install -o root -g 10010 -m 0440 {sources[name]} {SECRETS_DIR}/{name}" for name in NAMES),
        "install -o root -g root -m 0644 /opt/jobhunter/deploy/firewall/jobhunter-egress.service "
        "/etc/systemd/system/jobhunter-egress.service",
        "systemctl daemon-reload",
        "systemctl enable --now jobhunter-egress.service",
        "cd /opt/jobhunter",
        *(f"{COMPOSE} run --rm --no-deps {role} jobhunter startup-check --role {role}" for role in ROLES),
    ]
    if deleted:
        lines += [f"rm -f -- {source / name}" for name in NAMES if name not in empty]
    return lines


def test_the_names_are_the_secrets_of_the_spec() -> None:
    assert len(NAMES) == len(SECRET_NAMES) and set(NAMES) == SECRET_NAMES
    assert set(OPTIONAL) <= SECRET_NAMES


def test_the_steps_run_in_order(tmp_path: Path) -> None:
    run = dry_run(tmp_path, NAMES, "--delete-sources")
    assert (run.done.returncode, run.done.stderr) == (0, ""), run.done.stderr
    assert commands(run.done.stdout) == expected(run.source, deleted=True)
    # The preflight read the group, then the host users, then the swap areas; nothing else.
    assert run.probed == [GETENT, PASSWD, SWAPON]
    # A dry run deletes nothing: every source is still there, as it was.
    assert {path.name: path.read_text(encoding="utf-8") for path in run.source.iterdir()} == {
        name: f"{PLANTED}-{name}\n" for name in NAMES
    }


def test_a_missing_optional_secret_becomes_an_empty_file(tmp_path: Path) -> None:
    present = tuple(name for name in NAMES if name not in OPTIONAL)
    run = dry_run(tmp_path, present, "--delete-sources")
    assert (run.done.returncode, run.done.stderr) == (0, ""), run.done.stderr
    assert commands(run.done.stdout) == expected(run.source, empty=OPTIONAL, deleted=True)
    for name in OPTIONAL:  # never over a file that exists, such as the previous key during a rotation
        assert f"{NOTE}{name} is not in {run.source}: an empty file, unless {SECRETS_DIR}/{name} exists" in (
            run.done.stdout.splitlines()
        )


def test_it_never_prints_a_secret(tmp_path: Path) -> None:
    run = dry_run(tmp_path, NAMES, "--delete-sources")
    assert run.done.returncode == 0, run.done.stderr
    assert PLANTED not in run.done.stdout and PLANTED not in run.done.stderr


def test_it_never_uses_chmod_0400(tmp_path: Path) -> None:
    """§19.8 step 3: the app services run as uid 10001 and read their secrets through group 10010, so a file
    is 0440 and never 0400; the script sets each mode by `install -m`, never by chmod."""
    code = [
        line.split("#", 1)[0] for line in SCRIPT.read_text(encoding="utf-8").splitlines()[1:] if line.strip()
    ]
    assert not [line for line in code if "chmod" in line or "0400" in line or " 400" in line]
    run = dry_run(tmp_path, NAMES)
    assert run.done.returncode == 0, run.done.stderr
    printed = commands(run.done.stdout)
    assert not [line for line in printed if line.split()[0] == "chmod"]
    secret_installs = [line for line in printed if line.startswith("install ") and f"{SECRETS_DIR}/" in line]
    assert len(secret_installs) == len(NAMES)
    assert all(" -o root -g 10010 -m 0440 " in line for line in secret_installs)


def test_a_host_without_nft_stops_it(tmp_path: Path) -> None:
    """Step 4's unit loads the egress rule with `nft` (§16.9; final review M4): a host without it stops in the
    preflight, a dry run too, naming the package, before any step and before reading the group or the swap,
    so the secrets are never installed on a host whose rule cannot load."""
    run = dry_run(tmp_path, NAMES, "--delete-sources", nft=False)
    assert run.done.returncode == 1
    assert (commands(run.done.stdout), run.probed) == ([], [])
    assert run.done.stderr == (
        "host-setup: nft is not installed: install the package nftables (apt-get install nftables); step 4's "
        "egress rule needs it; nothing was changed\n"
    )


@pytest.mark.parametrize("missing", [name for name in NAMES if name not in OPTIONAL])
def test_a_missing_required_secret_stops_it(tmp_path: Path, missing: str) -> None:
    run = dry_run(tmp_path, tuple(name for name in NAMES if name != missing))
    assert run.done.returncode == 1
    # It stops in the preflight, before any step and before reading the group or the swap, naming the file.
    assert (commands(run.done.stdout), run.probed) == ([], [])
    assert run.done.stderr == (
        f"host-setup: {missing} is not in {run.source} (deploy/secrets.example lists it); "
        "nothing was changed\n"
    )


@pytest.mark.parametrize(
    ("entry", "stop"),
    [
        (("", 2), None),  # no group 10010: step 1 adds it
        (("jobhunter-secrets:x:10010:\n", 0), None),  # ours, with no members: kept
        (
            ("jobhunter-secrets:x:10010:alice,bob\n", 0),
            "the group jobhunter-secrets has members (alice,bob): no host user may be one (§19.2)",
        ),
        (("docker:x:10010:\n", 0), "gid 10010 is the group docker, not jobhunter-secrets"),
        (("", 1), "getent group 10010 failed (1)"),
    ],
    ids=["absent", "ours", "ours-with-members", "another-group", "getent-fails"],
)
def test_the_secrets_group(tmp_path: Path, entry: tuple[str, int], stop: str | None) -> None:
    """§19.2: no host user is a member of group 10010, which can read every secret. The preflight stops,
    before any change, unless the gid is free or is jobhunter-secrets with no members."""
    run = dry_run(tmp_path, NAMES, answers={"getent": {"10010": entry}})
    if stop is not None:
        assert run.done.returncode == 1
        assert run.done.stderr == f"host-setup: {stop}; nothing was changed\n"
        assert (commands(run.done.stdout), run.probed) == ([], [GETENT])
        return
    assert (run.done.returncode, run.done.stderr) == (0, ""), run.done.stderr
    assert commands(run.done.stdout) == expected(run.source, group=entry[1] == 2)
    assert run.probed == [GETENT, PASSWD, SWAPON]


MEMBERS: Final = ": no host user may be a member of jobhunter-secrets (§19.2)"


@pytest.mark.parametrize("group", [("", 2), ("jobhunter-secrets:x:10010:\n", 0)], ids=["absent", "ours"])
@pytest.mark.parametrize(
    ("passwd", "stop"),
    [
        ((OTHER_USERS, 0), None),
        (
            (f"{OTHER_USERS}alice:x:1001:10010::/home/alice:/bin/bash\n", 0),
            f"host users with gid 10010 as their primary group (alice){MEMBERS}",
        ),
        (
            (
                f"alice:x:1001:10010::/home/alice:/bin/bash\n{OTHER_USERS}bob:x:1002:10010::/home/bob:/bin/sh\n",
                0,
            ),
            f"host users with gid 10010 as their primary group (alice,bob){MEMBERS}",
        ),
        (("", 1), "getent passwd failed (1)"),
    ],
    ids=["other-gids", "one-with-gid-10010", "two-with-gid-10010", "getent-fails"],
)
def test_no_host_user_has_the_secrets_gid_as_primary_group(
    tmp_path: Path, group: tuple[str, int], passwd: tuple[str, int], stop: str | None
) -> None:
    """§19.2: a host user whose primary group is gid 10010 is a member of the secrets group too, so the
    preflight stops on one, before any change, whether the group exists yet or not; users with other gids
    pass (a uid of 10010 does not count)."""
    run = dry_run(tmp_path, NAMES, answers={"getent": {"10010": group, "passwd": passwd}})
    if stop is None:
        assert (run.done.returncode, run.done.stderr) == (0, ""), run.done.stderr
        assert f"{NOTE}no host user has gid 10010 as its primary group" in run.done.stdout.splitlines()
        assert commands(run.done.stdout) == expected(run.source, group=group[1] == 2)
        assert run.probed == [GETENT, PASSWD, SWAPON]
        return
    assert run.done.returncode == 1
    assert run.done.stderr == f"host-setup: {stop}; nothing was changed\n"
    assert (commands(run.done.stdout), run.probed) == ([], [GETENT, PASSWD])


@dataclass(frozen=True)
class Swap:
    """A swap check case: `swapon`'s list; `findmnt`'s answer for the swap file, if any; `lsblk`'s TYPE stack
    per device; the probe calls after `swapon`; the area the check stops on (`None`: it passes). `{file}` is
    the case's swap file, a real file in the test's directory named `file_name`; `{raw}` is its name as
    `swapon --raw` writes it."""

    swapon: str
    stack: dict[str, str] = field(default_factory=dict)
    findmnt: tuple[str, int] | None = None
    calls: tuple[str, ...] = ()
    stop: str | None = None
    file_name: str = "swapfile"


FINDMNT: Final = "findmnt -nro SOURCE --target {file}"
SWAP_CASES: Final = {
    "no-swap": Swap(swapon=""),
    "a-plain-partition": Swap(
        swapon="/dev/sda3\n",
        stack={"/dev/sda3": "part\ndisk\n"},
        calls=("lsblk -nrso TYPE -- /dev/sda3",),
        stop="/dev/sda3",
    ),
    "a-dm-crypt-partition": Swap(
        swapon="/dev/mapper/cryptswap\n",
        stack={"/dev/mapper/cryptswap": "crypt\npart\ndisk\n"},
        calls=("lsblk -nrso TYPE -- /dev/mapper/cryptswap",),
    ),
    "a-file-on-an-encrypted-root": Swap(
        swapon="{file}\n",
        findmnt=("/dev/mapper/root\n", 0),
        stack={"/dev/mapper/root": "crypt\npart\ndisk\n"},
        calls=(FINDMNT, "lsblk -nrso TYPE -- /dev/mapper/root"),
    ),
    "a-file-on-a-plain-btrfs-subvolume": Swap(
        swapon="{file}\n",
        findmnt=("/dev/sda2[/@swap]\n", 0),
        stack={"/dev/sda2": "part\ndisk\n"},
        calls=(FINDMNT, "lsblk -nrso TYPE -- /dev/sda2"),
        stop="{file}",
    ),
    "an-encrypted-area-and-a-plain-one": Swap(
        swapon="/dev/mapper/cryptswap\n/dev/sda3\n",
        stack={"/dev/mapper/cryptswap": "crypt\npart\ndisk\n", "/dev/sda3": "part\ndisk\n"},
        calls=("lsblk -nrso TYPE -- /dev/mapper/cryptswap", "lsblk -nrso TYPE -- /dev/sda3"),
        stop="/dev/sda3",
    ),
    "zram": Swap(
        swapon="/dev/zram0\n",
        stack={"/dev/zram0": "disk\n"},
        calls=("lsblk -nrso TYPE -- /dev/zram0",),
        stop="/dev/zram0",
    ),
    "findmnt-fails": Swap(swapon="{file}\n", findmnt=("", 1), calls=(FINDMNT,), stop="findmnt"),
    "a-file-whose-name-holds-a-space": Swap(
        swapon="{raw}\n",
        findmnt=("/dev/mapper/root\n", 0),
        stack={"/dev/mapper/root": "crypt\npart\ndisk\n"},
        calls=(FINDMNT, "lsblk -nrso TYPE -- /dev/mapper/root"),
        file_name="swap file",
    ),
}


@pytest.mark.parametrize("case", SWAP_CASES.values(), ids=SWAP_CASES.keys())
def test_the_swap_check(tmp_path: Path, case: Swap) -> None:
    """§19.8 step 5: no swap, or encrypted swap only. The preflight stops, before any change, on an area
    with no dm-crypt layer in its block device stack, judging a swap file by its filesystem's device."""
    swap_file = tmp_path / case.file_name
    swap_file.write_bytes(b"")

    def fill(text: str) -> str:
        return text.replace("{file}", str(swap_file)).replace("{raw}", str(swap_file).replace(" ", r"\x20"))

    answers: Answers = {
        "swapon": {"--raw": (fill(case.swapon), 0)},
        "lsblk": {device: (types, 0) for device, types in case.stack.items()},
    }
    if case.findmnt is not None:
        answers["findmnt"] = {str(swap_file): case.findmnt}
    run = dry_run(tmp_path, NAMES, answers=answers)
    assert run.probed == [GETENT, PASSWD, SWAPON, *(fill(call) for call in case.calls)]
    if case.stop is None:
        assert (run.done.returncode, run.done.stderr) == (0, ""), run.done.stderr
        assert SWAP_PASSED in run.done.stdout.splitlines()
        assert commands(run.done.stdout) == expected(run.source)
        return
    assert run.done.returncode == 1
    assert commands(run.done.stdout) == []  # nothing would have been run
    if case.stop == "findmnt":
        assert run.done.stderr == (
            f"host-setup: cannot find the device that holds the swap file {swap_file}; nothing was changed\n"
        )
    else:
        assert run.done.stderr == NOT_ENCRYPTED.format(fill(case.stop))
