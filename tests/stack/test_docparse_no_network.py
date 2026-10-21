"""The docparse service of the dev stack (spec §12.2, §19.1; Task 8): test N1's docparse half (§18.2).

Run explicitly with `-m stack`. Only the `docparse` service runs, as the compose project
`jobhunter-docparsetest` of its own: it has no network and publishes nothing, so it never collides with the
dev stack `jobhunter-dev` or the stack tests' `jobhunter-stacktest`. The fixture removes the project's
containers and volumes before the start (what a killed earlier run left behind) and after the module.
"""

import json
import subprocess
from collections.abc import Iterator
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
(job / 'request.tmp').write_text(json.dumps({'v': 1, 'mime': 'application/pdf', 'input': 'input.pdf',
                                             'mode': 'extract'}))
(job / 'request.tmp').rename(job / 'request.json')
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
