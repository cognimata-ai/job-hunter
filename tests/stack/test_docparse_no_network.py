"""The docparse service of the dev stack (spec §12.2, §19.1; Task 8): test N1's docparse half (§18.2).

Run explicitly with `-m stack`. Only the `docparse` service runs, as the compose project
`jobhunter-docparsetest` of its own: it has no network and publishes nothing, so it never collides with the
dev stack `jobhunter-dev` or the stack tests' `jobhunter-stacktest`. The fixture removes the project's
containers and volumes before the start (what a killed earlier run left behind) and after the module.
"""

import json
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.support.profile import minimal_pdf

pytestmark = pytest.mark.stack
REPO = Path(__file__).resolve().parents[2]
PROJECT = "jobhunter-docparsetest"
DC = [
    "docker",
    "compose",
    "-p",
    PROJECT,
    "--env-file",
    str(REPO / "deploy/images.lock"),
    "-f",
    str(REPO / "deploy/compose.dev.yaml"),
]
CALL_TIMEOUT_S = 120
# The probes exit 3 when the call fails with an OSError (socket.gaierror is one), 0 when it succeeds; any
# other failure of the probe itself exits 1 with its traceback.
TCP_PROBE = (
    "import socket, sys\ntry:\n socket.create_connection(('1.1.1.1', 53), 2)\nexcept OSError:\n sys.exit(3)"
)
DNS_PROBE = "import socket, sys\ntry:\n socket.getaddrinfo('example.com', 443)\nexcept OSError:\n sys.exit(3)"
# A job written by hand as the worker writes it, on the service's own parse root: the PDF comes on stdin.
ROUND_TRIP = """
import json, os, pathlib, sys, time
job = pathlib.Path('/data/parse/stacktest-job')
job.mkdir(mode=0o770)
os.chmod(job, 0o2770)
(job / 'input.pdf').write_bytes(sys.stdin.buffer.read())
(job / 'request.json.tmp').write_text(json.dumps({'v': 1, 'mime': 'application/pdf', 'input': 'input.pdf',
                                                  'mode': 'extract'}))
(job / 'request.json.tmp').rename(job / 'request.json')  # the worker's temporary name: the sweep keeps it
deadline = time.monotonic() + 60
while not (job / 'result.json').exists():
    assert time.monotonic() < deadline, 'no result'
    time.sleep(0.1)
print(json.dumps({'result': json.loads((job / 'result.json').read_text()),
                  'text': (job / 'text.txt').read_text(encoding='utf-8')}))
"""


def _remove() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*DC, "down", "-v", "--remove-orphans"], capture_output=True, text=True, check=False, timeout=300
    )


@pytest.fixture(scope="module")
def docparse() -> Iterator[None]:
    _remove()
    try:
        up = subprocess.run(
            [*DC, "up", "-d", "--build", "--wait", "docparse"], capture_output=True, text=True, timeout=900
        )
        assert up.returncode == 0, up.stdout + up.stderr
        yield
    finally:
        down = _remove()
    assert down.returncode == 0, down.stderr


def exec_(*command: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*DC, "exec", "-T", "docparse", *command], input=stdin, capture_output=True, timeout=CALL_TIMEOUT_S
    )


def test_n1_docparse_can_neither_connect_nor_resolve_an_outside_address(docparse) -> None:
    # Control: the container runs the probes, and it has the loopback interface only.
    names = exec_("python", "-c", "import socket; print([n for _, n in socket.if_nameindex()])")
    assert names.returncode == 0 and names.stdout.decode().strip() == "['lo']", names
    tcp = exec_("python", "-c", TCP_PROBE)
    assert tcp.returncode == 3, tcp
    dns = exec_("python", "-c", DNS_PROBE)
    assert dns.returncode == 3, dns


def test_docparse_runs_unprivileged_on_a_read_only_root_with_only_the_parse_group(docparse) -> None:
    assert exec_("id", "-u").stdout.split() == [b"10002"]
    assert sorted(exec_("id", "-G").stdout.split()) == [b"10002", b"10011"]  # never the secrets group 10010
    assert exec_("stat", "-c", "%a %u %g", "/data/parse").stdout.split() == [b"2770", b"0", b"10011"]
    # The tmpfs of §19.1: size=256m, so a flood of job files cannot take the host's memory.
    size = exec_("python", "-c", "import os; s = os.statvfs('/data/parse'); print(s.f_blocks * s.f_frsize)")
    assert size.stdout.split() == [str(256 * 2**20).encode()]
    assert exec_("ls", "-A", "/run/secrets").returncode != 0  # no secret is mounted
    assert exec_("touch", "/app/x").returncode != 0  # read-only root


def test_the_image_holds_the_whole_docparse_package_and_nothing_else(docparse) -> None:
    listed = exec_("python", "-c", "import os; print(sorted(os.listdir('/app/docparse')))")
    expected = sorted(p.name for p in (REPO / "docparse").glob("*.py"))
    assert listed.stdout.decode().strip() == str(expected)  # every module, and no Dockerfile or cache


def test_the_docparse_image_extracts_a_job_on_its_parse_root(docparse) -> None:
    lines = ["Max Mustermann", "Softwareentwickler bei der Musterfirma GmbH"]
    r = exec_("python", "-c", ROUND_TRIP, stdin=minimal_pdf(lines))
    assert r.returncode == 0, r.stderr.decode()
    answer = json.loads(r.stdout)
    assert answer["result"] | {"chars": None} == {
        "v": 1,
        "status": "ok",
        "code": None,
        "method": "pdfplumber",  # under 300 characters: pdfplumber took over
        "page_count": 1,
        "chars": None,
    }
    assert answer["result"]["chars"] == len(answer["text"])
    assert [line.strip() for line in answer["text"].splitlines()] == lines


# --- a compromised parse in the real sandbox (final review FR-11) ------------------------------------------


def container_id() -> str:
    found = subprocess.run([*DC, "ps", "--all", "--quiet", "docparse"], capture_output=True, text=True)
    (container,) = found.stdout.split()
    return container


def inspect(field: str) -> str:
    done = subprocess.run(
        ["docker", "inspect", "--format", f"{{{{{field}}}}}", container_id()], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def exec_as(user: str, *command: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*DC, "exec", "-T", "--user", user, "docparse", *command],
        input=stdin,
        capture_output=True,
        timeout=CALL_TIMEOUT_S,
    )


def test_docparse_runs_under_a_process_limit(docparse) -> None:
    assert inspect(".HostConfig.PidsLimit") == "16"


# Hostile extractors, written to the container's own /tmp and imported from there by the spawn child.
HOSTILE = """
import errno, os, resource, threading, time
from docparse.extract import Extracted

def try_to_start(path, mime):
    try:
        pid = os.fork()
    except OSError as exc:
        forked = "refused:" + errno.errorcode[exc.errno]
    else:
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        forked = "started"
    try:
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        threaded = "started"
    except RuntimeError:
        threaded = "refused"
    return Extracted(f"fork={forked} thread={threaded}", "pypdf", 1)

def fork_and_linger(path, mime):
    pid = os.fork()
    if pid == 0:
        os.closerange(3, resource.getrlimit(resource.RLIMIT_NOFILE)[0])
        time.sleep(120)
        os._exit(0)
    return Extracted(f"grandchild={pid}", "pypdf", 1)
"""
# Two jobs on the container's /tmp (the loop serves /data/parse only), each run as the loop runs it: under the
# default limits, and with RLIMIT_NPROC left as it is so that the group kill has a process to kill.
PROCESS_PROBE = """
import json, os, pathlib, sys, time
sys.path.insert(0, '/tmp')
import hostile
from docparse.worker import Limits, run_job

def job(name, data):
    path = pathlib.Path('/tmp') / name
    path.mkdir()
    (path / 'input.pdf').write_bytes(data)
    (path / 'request.json').write_text(json.dumps({'v': 1, 'mime': 'application/pdf', 'input': 'input.pdf',
                                                    'mode': 'extract'}))
    return path

def alive(pid):
    try:
        state = pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state not in ('Z', 'X')

data = sys.stdin.buffer.read()
limited = job('limited', data)
run_job(limited, extractor=hostile.try_to_start)
forking = job('forking', data)
run_job(forking, extractor=hostile.fork_and_linger,
        limits=Limits(cpu_s=30, as_bytes=768 * 2**20, wall_s=5, processes=None))
pid = int((forking / 'text.txt').read_text().removeprefix('grandchild='))
deadline = time.monotonic() + 5
while alive(pid) and time.monotonic() < deadline:
    time.sleep(0.05)
print(json.dumps({'limited': (limited / 'text.txt').read_text(), 'grandchild_alive': alive(pid)}))
"""


def test_in_the_image_a_parse_can_start_no_process_and_what_it_started_dies_with_it(docparse) -> None:
    """(a) and (b) as uid 10002 without capabilities: RLIMIT_NPROC refuses a fork and a thread, and with the
    limit lifted the process the parse forked dies with the job (the group kill)."""
    written = exec_("sh", "-c", "cat > /tmp/hostile.py", stdin=HOSTILE.encode())
    assert written.returncode == 0, written.stderr.decode()
    probe = exec_("python", "-c", PROCESS_PROBE, stdin=minimal_pdf(["Max Mustermann"]))
    assert probe.returncode == 0, probe.stderr.decode()
    assert json.loads(probe.stdout) == {
        "limited": "fork=refused:EAGAIN thread=refused",
        "grandchild_alive": False,
    }


# A job written as the worker writes it, with a compromised parse's leftovers planted beside it first: a file
# on the root and a directory of docparse's uid that no one may enter. It waits until the job is answered
# and both leftovers are gone.
LITTERED_JOB = """
import json, os, pathlib, sys, time
root = pathlib.Path('/data/parse')
job = root / 'littered-job'
job.mkdir(mode=0o770)
os.chmod(job, 0o2770)
data = sys.stdin.buffer.read()
(job / 'input.pdf').write_bytes(data)
stash = job / 'stash'
stash.mkdir()
(stash / 'copy.pdf').write_bytes(data)
stash.chmod(0)
(root / 'stray.pdf').write_bytes(data)
(job / 'request.json.tmp').write_text(json.dumps({'v': 1, 'mime': 'application/pdf', 'input': 'input.pdf',
                                                  'mode': 'extract'}))
(job / 'request.json.tmp').rename(job / 'request.json')
deadline = time.monotonic() + 60
while stash.exists() or (root / 'stray.pdf').exists() or not (job / 'result.json').exists():
    assert time.monotonic() < deadline, sorted(os.listdir(job))
    time.sleep(0.1)
print(json.dumps({'status': json.loads((job / 'result.json').read_text())['status'],
                  'job': sorted(os.listdir(job)), 'stray': (root / 'stray.pdf').exists()}))
"""


def test_docparse_removes_the_leftovers_beside_a_job_that_the_worker_could_not(docparse) -> None:
    """(d) As uid 10002: a directory of its own uid with mode 0 (the worker, 10001, could not remove it) and a
    file on the root are gone once the job is answered; the job keeps its protocol files."""
    r = exec_("python", "-c", LITTERED_JOB, stdin=minimal_pdf(["Max Mustermann"]))
    assert r.returncode == 0, r.stderr.decode()
    assert json.loads(r.stdout) == {
        "status": "ok",
        "job": ["input.pdf", "request.claimed", "result.json", "text.txt"],
        "stray": False,
    }


NEW_JOB = """
import os, pathlib, sys
job = pathlib.Path('/data/parse') / sys.argv[1]
job.mkdir(mode=0o770)
os.chmod(job, 0o2770)
(job / 'input.pdf').write_bytes(sys.stdin.buffer.read())
"""
PUBLISH = """
import json, pathlib, sys
job = pathlib.Path('/data/parse') / sys.argv[1]
(job / 'request.json.tmp').write_text(json.dumps({'v': 1, 'mime': 'application/pdf', 'input': 'input.pdf',
                                                  'mode': 'extract'}))
(job / 'request.json.tmp').rename(job / 'request.json')
"""


def until(condition: Callable[[], bool], timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.25)


def test_a_leftover_docparse_cannot_remove_stops_it_and_compose_restarts_it(docparse) -> None:
    """(e) A directory of root's, with a file in it, beside a job's protocol files: uid 10002 can neither
    empty nor open it up. After the job the sweep fails, docparse exits 70 and the restart policy starts it
    again; once the entry is gone (the operator's recreated sandbox), it serves again. Here docparse alone
    holds the parse_jobs tmpfs, so the restart remounts it empty; in the dev stack the worker keeps it
    mounted, and the restarted docparse fails its first sweep again until the sandbox is recreated."""
    restarts = int(inspect(".RestartCount"))
    data = minimal_pdf(["Max Mustermann"])
    assert exec_("python", "-c", NEW_JOB, "stuck-job", stdin=data).returncode == 0
    planted = exec_as(
        "0:10011",
        "sh",
        "-c",
        "umask 022 && mkdir -m 0755 /data/parse/stuck-job/rooted && echo x >/data/parse/stuck-job/rooted/f",
    )
    assert planted.returncode == 0, planted.stderr.decode()
    assert exec_("python", "-c", PUBLISH, "stuck-job").returncode == 0
    until(lambda: int(inspect(".RestartCount")) > restarts, 60, "docparse was not restarted")
    logs = subprocess.run(["docker", "logs", container_id()], capture_output=True, text=True).stderr
    assert "docparse.leftover_not_removed error=PermissionError" in logs
    assert "docparse.sweep_failed failures=1" in logs
    sweep_lines = [line for line in logs.splitlines() if "leftover" in line or "sweep_failed" in line]
    assert sweep_lines and not [line for line in sweep_lines if "stuck" in line or "rooted" in line]

    def removed() -> bool:  # the container may be between two restarts
        gone = exec_as("0:10011", "rm", "-rf", "/data/parse/stuck-job")
        return gone.returncode == 0

    until(removed, 120, "the planted entry could not be removed")
    until(lambda: inspect(".State.Status") == "running", 120, "docparse did not come back")
    r = exec_("python", "-c", ROUND_TRIP.replace("stacktest-job", "after-restart-job"), stdin=data)
    assert r.returncode == 0, r.stderr.decode()
    assert json.loads(r.stdout)["result"]["status"] == "ok"
