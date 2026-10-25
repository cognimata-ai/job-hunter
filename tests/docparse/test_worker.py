"""The docparse job runner and loop (spec §12.2; Task 8): the claim, the rlimited spawn child, the atomic
results and the loop that logs only job ids and codes.

Run with `uv run --group docparse pytest -W error -m docparse tests/docparse`. The extractors that run in the
child live in `tests.docparse.conftest`, which the spawn child imports by module path.
"""

import errno
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

import docparse.worker as worker
from docparse.worker import Limits, claim, run_job, serve
from tests.docparse import conftest
from tests.docparse.conftest import (
    MARKED_TEXT,
    SECRET_TEXT,
    burn,
    close_all_and_linger,
    die,
    explode,
    flood,
    hog,
    make_job,
    mark,
    report_inherited,
    result_of,
    sleeper,
    stall_mid_answer,
)
from tests.support.profile import DOCX_MIME, PDF_MIME, docx_bytes, minimal_pdf

pytestmark = pytest.mark.docparse

REPO = Path(__file__).resolve().parents[2]
LIMIT_WAIT_S = 5
LOOP_WAIT_S = 30


def wait_for(path: Path, timeout_s: float = LOOP_WAIT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)


def test_rlimit_kills_a_runaway_parse(tmp_path) -> None:
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), "application/pdf")
    started = time.monotonic()
    run_job(job, extractor=burn, limits=Limits(cpu_s=1, as_bytes=256 * 2**20))
    result = json.loads((job / "result.json").read_text())
    assert result["status"] == "error" and result["code"] == "parse_limit" and time.monotonic() - started < 5


def test_the_cpu_rlimit_alone_ends_a_runaway_parse(tmp_path) -> None:
    """The wall-clock limit is far away, so only RLIMIT_CPU can end the child within the 5 s."""
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    started = time.monotonic()
    run_job(job, extractor=burn, limits=Limits(cpu_s=1, as_bytes=256 * 2**20, wall_s=120))
    assert result_of(job)["code"] == "parse_limit" and time.monotonic() - started < LIMIT_WAIT_S


def test_the_address_space_rlimit_ends_a_memory_hog(tmp_path) -> None:
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    run_job(job, extractor=hog, limits=Limits(cpu_s=30, as_bytes=256 * 2**20))
    assert result_of(job) == {
        "v": 1,
        "status": "error",
        "code": "parse_limit",
        "method": "pypdf",
        "page_count": None,
        "chars": 0,
    }


def test_an_answer_over_the_size_bound_is_a_parse_limit(tmp_path) -> None:
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    run_job(job, extractor=flood)
    assert result_of(job)["code"] == "parse_limit" and not (job / "text.txt").exists()


def test_the_wall_clock_limit_ends_a_parse_that_blocks(tmp_path) -> None:
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    started = time.monotonic()
    run_job(job, extractor=sleeper, limits=Limits(cpu_s=30, as_bytes=768 * 2**20, wall_s=1))
    assert result_of(job)["code"] == "parse_limit" and time.monotonic() - started < LIMIT_WAIT_S


def test_the_child_is_a_fresh_spawned_interpreter_never_a_fork_of_the_loop(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(conftest.LOOP_PROCESS_STATE, "set_in_the_loop", True)
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    run_job(job, extractor=report_inherited)
    assert (job / "text.txt").read_text(encoding="utf-8") == "inherited=False"


def test_a_child_that_stops_mid_answer_is_ended_at_the_deadline(tmp_path) -> None:
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    limits = Limits(cpu_s=30, as_bytes=768 * 2**20, wall_s=1)
    # In a daemon thread: a loop that waited for the rest of the answer for ever fails here, never hangs.
    runner = threading.Thread(
        target=run_job, args=(job,), kwargs={"extractor": stall_mid_answer, "limits": limits}
    )
    runner.daemon = True
    runner.start()
    runner.join(LIMIT_WAIT_S)
    assert not runner.is_alive(), "the loop still waits for the rest of the answer"
    assert result_of(job)["code"] == "parse_limit" and not (job / "text.txt").exists()


def test_a_child_that_closes_its_pipes_and_lingers_is_killed_within_the_bound(tmp_path) -> None:
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    limits = Limits(cpu_s=30, as_bytes=768 * 2**20, wall_s=1)
    bound = worker.SPAWN_MARGIN_S + limits.wall_s + worker.EXIT_JOIN_S
    runner = threading.Thread(
        target=run_job, args=(job,), kwargs={"extractor": close_all_and_linger, "limits": limits}, daemon=True
    )
    started = time.monotonic()
    runner.start()
    runner.join(bound + LIMIT_WAIT_S)  # a reap that waits for the child's exit fails here, never hangs
    assert not runner.is_alive(), "run_job still waits for a child that closed its pipes"
    assert time.monotonic() - started < bound + 1  # the bound, plus the child's start
    assert result_of(job)["code"] == "parse_limit"


def no_pipe_for_the_child(monkeypatch) -> None:
    """The first os.pipe (the answer pipe) is made; the next, inside child.start(), fails with EMFILE."""
    real_pipe, calls = os.pipe, []

    def pipe() -> tuple[int, int]:
        calls.append(1)
        if len(calls) > 1:
            raise OSError(errno.EMFILE, "Too many open files")
        return real_pipe()

    monkeypatch.setattr(os, "pipe", pipe)


@pytest.mark.parametrize("failure", ["unpicklable", "emfile"])
def test_a_child_that_cannot_start_gives_a_parse_error_at_once(
    tmp_path, monkeypatch, caplog, failure
) -> None:
    caplog.set_level(logging.INFO)
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    if failure == "emfile":
        no_pipe_for_the_child(monkeypatch)
        run_job(job, extractor=mark)
    else:
        run_job(job, extractor=lambda path, mime: mark(path, mime))  # a lambda never reaches a spawn child
    monkeypatch.undo()
    assert result_of(job)["code"] == "parse_error" and not list(job.glob("ran-*"))
    failed = [
        r.getMessage() for r in caplog.records if r.name == "docparse" and "child_failed" in r.getMessage()
    ]
    error = "OSError" if failure == "emfile" else "AttributeError"  # "Can't pickle local object"
    assert failed == [f"docparse.child_failed job_id={job.name} error={error}"]


def test_a_failing_parse_is_a_parse_error_and_only_its_job_id_and_code_are_logged(
    tmp_path, caplog, capfd
) -> None:
    caplog.set_level(logging.DEBUG)
    failed = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME, name="job-failed")
    died = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME, name="job-died")
    run_job(failed, extractor=explode)
    run_job(died, extractor=die)
    assert result_of(failed)["code"] == "parse_error" and result_of(died)["code"] == "parse_error"
    assert [r.getMessage() for r in caplog.records if r.name == "docparse"] == [
        "docparse.job_done job_id=job-failed status=error code=parse_error",
        "docparse.job_done job_id=job-died status=error code=parse_error",
    ]
    out, err = capfd.readouterr()
    assert "Mustermann" not in out + err + caplog.text  # the child neither prints nor logs the document


def test_the_claim_is_a_rename_so_a_second_worker_cannot_claim_the_job(tmp_path) -> None:
    job = make_job(tmp_path, minimal_pdf(["Erika Musterfrau"]), PDF_MIME)
    assert claim(job) is True
    assert not (job / "request.json").exists() and (job / "request.claimed").exists()
    assert claim(job) is False
    run_job(job, extractor=mark)  # a second worker: the job is claimed, nothing runs and nothing is written
    assert not list(job.glob("ran-*")) and not (job / "result.json").exists()


def test_of_racing_workers_exactly_one_claims_and_extracts(tmp_path) -> None:
    job = make_job(tmp_path, minimal_pdf(["Erika Musterfrau"]), PDF_MIME)
    start = threading.Barrier(4)

    def racer() -> None:
        start.wait()
        run_job(job, extractor=mark)

    racers = [threading.Thread(target=racer) for _ in range(4)]
    for thread in racers:
        thread.start()
    for thread in racers:
        thread.join(LOOP_WAIT_S)
    assert len(list(job.glob("ran-*"))) == 1
    assert result_of(job)["status"] == "ok"
    assert (job / "text.txt").read_text(encoding="utf-8") == MARKED_TEXT


def test_result_json_appears_only_after_text_txt_is_complete(tmp_path, monkeypatch) -> None:
    job = make_job(tmp_path, minimal_pdf(["Erika Musterfrau"]), PDF_MIME)
    renames: list[tuple[str, str]] = []
    real_replace = os.replace

    def recording_replace(src, dst) -> None:
        src, dst = Path(src), Path(dst)
        if dst.name == "result.json":
            # At the instant result.json is published, text.txt is in place with all of the text.
            result = json.loads(src.read_text())
            text = (dst.parent / "text.txt").read_text(encoding="utf-8")
            assert text == MARKED_TEXT and len(text) == result["chars"]
        renames.append((src.name, dst.name))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    run_job(job, extractor=mark)
    monkeypatch.undo()
    # Both are written to a temporary name and renamed: neither is ever seen half-written.
    published = [(src, dst) for src, dst in renames if dst in ("text.txt", "result.json")]
    assert published == [("text.txt.tmp", "text.txt"), ("result.json.tmp", "result.json")]
    assert sorted(p.name for p in job.iterdir() if not p.name.startswith("ran-")) == [
        "input.pdf",
        "request.claimed",
        "result.json",
        "text.txt",
    ]
    assert {oct(p.stat().st_mode & 0o777) for p in (job / "text.txt", job / "result.json")} == {"0o660"}


def test_validate_mode_checks_the_pdf_and_writes_no_text(tmp_path) -> None:
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"] * 3, lines_per_page=1), PDF_MIME, mode="validate")
    run_job(job)
    assert result_of(job) == {
        "v": 1,
        "status": "ok",
        "code": None,
        "method": "pypdf",
        "page_count": 3,
        "chars": 0,
    }
    assert not (job / "text.txt").exists()


@pytest.mark.parametrize(
    ("change", "method"),
    [
        ({"mime": "text/plain", "input": "input.pdf"}, None),
        ({"mode": "validate"}, "docx"),  # validate checks a PDF only
        ({"mode": "ocr"}, "docx"),
        ({"v": 2}, "docx"),
        ({"input": "../input.docx"}, "docx"),
        ({"input": "input.pdf"}, "docx"),  # the input name must match the type
    ],
)
def test_an_unsupported_request_is_refused_without_extracting(tmp_path, change, method) -> None:
    job = make_job(tmp_path, docx_bytes(["Erika Musterfrau"]), DOCX_MIME)
    request = json.loads((job / "request.json").read_text()) | change
    (job / "request.json").write_text(json.dumps(request))
    run_job(job, extractor=mark)
    assert result_of(job) == {
        "v": 1,
        "status": "error",
        "code": "unsupported",
        "method": method,
        "page_count": None,
        "chars": 0,
    }
    assert not list(job.glob("ran-*"))


def test_serve_runs_every_pending_job_without_a_socket_until_stopped(tmp_path, monkeypatch) -> None:
    def no_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the docparse loop opened a socket")

    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(socket, "socketpair", no_socket)
    pdf = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME, name="job-pdf")
    stop = threading.Event()
    loop = threading.Thread(target=serve, args=(tmp_path,), kwargs={"stop": stop})
    loop.start()
    try:
        wait_for(pdf / "result.json")
        docx = make_job(tmp_path, docx_bytes(["Erika Musterfrau"]), DOCX_MIME, name="job-docx")
        wait_for(docx / "result.json")  # a job published while the loop runs
    finally:
        stop.set()
        loop.join(LOOP_WAIT_S)
    assert not loop.is_alive()
    assert result_of(pdf)["status"] == "ok" and result_of(docx)["status"] == "ok"
    assert (pdf / "text.txt").read_text(encoding="utf-8").strip() == "Max Mustermann"
    assert (docx / "text.txt").read_text(encoding="utf-8") == "Erika Musterfrau"


def test_serve_survives_a_job_that_raises_and_logs_only_its_id_and_error_class(
    tmp_path, monkeypatch, caplog
) -> None:
    caplog.set_level(logging.INFO)
    real_run_job = worker.run_job

    def flaky(job_dir: Path) -> None:
        if job_dir.name == "job-a" and claim(job_dir):
            raise OSError(f"cannot write {SECRET_TEXT}")  # after its claim, as a vanished directory would
        real_run_job(job_dir)

    monkeypatch.setattr(worker, "run_job", flaky)
    first = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME, name="job-a")
    os.utime(first / "request.json", (1, 1))  # the oldest request is taken first
    second = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME, name="job-b")
    stop = threading.Event()
    loop = threading.Thread(target=serve, args=(tmp_path,), kwargs={"stop": stop})
    loop.start()
    try:
        wait_for(second / "result.json")
    finally:
        stop.set()
        loop.join(LOOP_WAIT_S)
    assert not (first / "result.json").exists()
    messages = [r.getMessage() for r in caplog.records if r.name == "docparse"]
    assert "docparse.job_failed job_id=job-a error=OSError" in messages
    assert "Mustermann" not in caplog.text


# --- a compromised parse: its processes and its leftovers (final review FR-11) -----------------------------

NOBODY = 65534
# The child's limits with RLIMIT_NPROC left as it is, so a test of the group kill can start a process
# whatever its uid (a non-root uid starts none under the default limits).
FORKING = Limits(cpu_s=30, as_bytes=768 * 2**20, wall_s=1, processes=None)


def alive(pid: int) -> bool:
    """Whether `pid` runs: a process that was killed and not yet reaped by its new parent is a zombie."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state not in ("Z", "X")


def gone(pid: int, timeout_s: float = LIMIT_WAIT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


@pytest.fixture
def unprivileged_root(tmp_path) -> Iterator[Path]:
    """A directory that the uid of `as_unprivileged()` owns. As root, that uid is nobody: root starts
    processes whatever its RLIMIT_NPROC and opens files whatever their modes, so only another uid shows
    either limit."""
    if os.geteuid() != 0:
        yield tmp_path
        return
    shared = Path(tempfile.mkdtemp(prefix="docparse-unprivileged-", dir="/tmp"))
    try:
        os.chown(shared, NOBODY, NOBODY)
        yield shared
    finally:
        shutil.rmtree(shared, ignore_errors=True)


def as_unprivileged() -> dict[str, Any]:
    """The `subprocess.run` arguments that run a command as a non-root uid: the test's own, or nobody."""
    if os.geteuid() != 0:
        return {}
    return {"user": NOBODY, "group": NOBODY, "extra_groups": []}


def chown_tree(path: Path) -> None:
    """`path` and everything under it to the uid of `as_unprivileged()`."""
    if os.geteuid() == 0:
        for directory, subdirectories, files in os.walk(path):
            for entry in [Path(directory), *(Path(directory) / n for n in [*subdirectories, *files])]:
                os.chown(entry, NOBODY, NOBODY, follow_symlinks=False)


def run_unprivileged(
    script: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """`python -c script args…` in the repository, as the uid of `as_unprivileged()`."""
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1", **(env or {})},
        capture_output=True,
        text=True,
        timeout=LOOP_WAIT_S * 2,
        check=False,
        **as_unprivileged(),
    )


@pytest.mark.parametrize("ending", ["answered", "overran"])
def test_a_process_the_parse_started_is_killed_with_its_job(tmp_path, ending) -> None:
    """(a) The child leads a process group of its own (`os.setsid`, its first statement), and the reap kills
    the group: a process the parse forked, which let go of the answer pipe, dies with the job whether the
    child answered and exited or was killed at its deadline."""
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    if ending == "answered":
        run_job(job, extractor=conftest.fork_and_linger, limits=FORKING)
        assert result_of(job)["status"] == "ok"
        pid = int((job / "text.txt").read_text().removeprefix("grandchild="))
    else:
        run_job(job, extractor=conftest.fork_and_overrun, limits=FORKING)
        assert result_of(job)["code"] == "parse_limit"
        pid = int((job / conftest.GRANDCHILD_PID).read_text())
    try:
        assert gone(pid), "the process the parse started outlived its job"
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def test_the_child_leads_a_process_group_of_its_own_and_the_loop_stays_in_its_own(tmp_path) -> None:
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    run_job(job, extractor=conftest.report_groups)
    pid, pgid, sid = map(int, (job / "text.txt").read_text().split())
    assert pgid == pid == sid  # a session and group of its own: a group kill reaches only the parse
    assert os.getpgid(0) != pid


START_SCRIPT = """
import sys
from pathlib import Path
from docparse.worker import run_job
from tests.docparse.conftest import try_to_start
run_job(Path(sys.argv[1]), extractor=try_to_start)
"""


def test_the_parse_child_may_start_no_process_and_no_thread(unprivileged_root) -> None:
    """(b) RLIMIT_NPROC 0: a non-root child can fork nothing and start no thread, so no descendant can leave
    its group. Root is exempt from the limit, so as root the job runs as nobody."""
    job = make_job(unprivileged_root, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    chown_tree(unprivileged_root)
    done = run_unprivileged(START_SCRIPT, str(job))
    assert done.returncode == 0, done.stderr
    assert (job / "text.txt").read_text() == "fork=refused:EAGAIN thread=refused"


EXTRACT_SCRIPT = """
import sys
from pathlib import Path
from docparse.worker import run_job
for job in sys.argv[1:]:
    run_job(Path(job))
"""


def test_a_real_parse_runs_under_the_process_limit(unprivileged_root) -> None:
    """The real extractors (pypdf, its pdfplumber fallback, python-docx) start no process or thread."""
    lines = [f"Erika Musterfrau, Zeile {n}" for n in range(30)]
    long_pdf = make_job(unprivileged_root, minimal_pdf(lines), PDF_MIME, name="long")
    short_pdf = make_job(unprivileged_root, minimal_pdf(["Max Mustermann"]), PDF_MIME, name="short")
    docx = make_job(unprivileged_root, docx_bytes(["Erika Musterfrau"]), DOCX_MIME, name="docx")
    chown_tree(unprivileged_root)
    done = run_unprivileged(EXTRACT_SCRIPT, *(str(j) for j in (long_pdf, short_pdf, docx)))
    assert done.returncode == 0, done.stderr
    assert [result_of(j)["method"] for j in (long_pdf, short_pdf, docx)] == ["pypdf", "pdfplumber", "docx"]
    assert {result_of(j)["status"] for j in (long_pdf, short_pdf, docx)} == {"ok"}
    assert (docx / "text.txt").read_text() == "Erika Musterfrau"


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def holding(root: Path, data: bytes) -> list[str]:
    """Every regular file under `root` (no symlink followed) whose content is `data`, relative to `root`."""
    found = []
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink() and path.is_file() and path.read_bytes() == data:
                found.append(str(path.relative_to(root)))
    return sorted(found)


def finished_job(root: Path, name: str, data: bytes) -> Path:
    """A job docparse has answered and the worker has not yet removed."""
    job = make_job(root, data, PDF_MIME, name=name)
    (job / "request.json").rename(job / "request.claimed")
    (job / "text.txt").write_text(MARKED_TEXT)
    (job / "result.json").write_text("{}")
    return job


def writing_job(root: Path, name: str, data: bytes) -> Path:
    """A job the worker is writing: its input, its request under the temporary name, not yet published."""
    job = root / name
    job.mkdir(mode=0o770)
    (job / "input.pdf").write_bytes(data)
    (job / "request.json.tmp").write_text("{}")
    return job


def serving(
    root: Path, monkeypatch, extractor
) -> tuple[threading.Thread, threading.Event, list[BaseException]]:
    """A `serve` loop on `root` whose jobs run `extractor`; the exception that ended it lands in the list."""
    real_run_job = worker.run_job
    monkeypatch.setattr(worker, "run_job", lambda job_dir: real_run_job(job_dir, extractor=extractor))
    stop, raised = threading.Event(), []

    def target() -> None:
        try:
            serve(root, stop=stop)
        except BaseException as exc:  # noqa: BLE001 - asserted by the test
            raised.append(exc)

    loop = threading.Thread(target=target, daemon=True)
    loop.start()
    return loop, stop, raised


def test_after_a_job_the_loop_removes_what_its_parse_left_and_keeps_the_worker_s_jobs(
    tmp_path, monkeypatch
) -> None:
    """(d) After each job: every non-protocol entry of the job, and every entry of the root that is no
    directory, goes at once (the subdirectory only its owner may enter, the FIFO, the hard link, the symlink);
    a directory younger than a worker could wait for keeps its protocol files only, so a job the worker is
    still writing and one whose answer it has not yet read are left as they are."""
    data = minimal_pdf(["Erika Musterfrau"])
    job = make_job(tmp_path, data, PDF_MIME, name="job-litter")
    writing = writing_job(tmp_path, "job-writing", data)
    answered = finished_job(tmp_path, "job-answered", data)
    before = {p.name: names(p) for p in (writing, answered)}
    loop, stop, raised = serving(tmp_path, monkeypatch, conftest.litter)
    try:
        wait_for(job / "result.json")
    finally:
        stop.set()
        loop.join(LOOP_WAIT_S)
    assert not loop.is_alive() and raised == []
    assert names(job) == ["input.pdf", "request.claimed", "result.json", "text.txt"]
    assert names(tmp_path) == ["hidden", "job-answered", "job-litter", "job-writing", "mimic"]
    assert names(tmp_path / "hidden") == []
    assert {p.name: names(p) for p in (writing, answered)} == before
    # Left, until it is older than a worker waits: a copy under a protocol name, as a job would hold it.
    assert holding(tmp_path, data) == [
        "job-answered/input.pdf",
        "job-litter/input.pdf",
        "job-writing/input.pdf",
        "mimic/input.pdf",
    ]


def deep_tree(base: Path, depth: int, data: bytes) -> None:
    """`base/d/d/…/d/input.pdf`, `depth` directories deep, made through descriptors (no path that long)."""
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            inner = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = inner
        out = os.open("input.pdf", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
        os.write(out, data)
        os.close(out)
    finally:
        os.close(fd)


def test_an_entry_unchanged_for_longer_than_a_worker_waits_is_removed_whatever_it_holds(tmp_path) -> None:
    """(d) An entry of the root unchanged (its ctime) for `LEFTOVER_AFTER_S` is no live job's: the worker gave
    up on it after its own timeout. Only the live job stays, with its protocol files."""
    data = minimal_pdf(["Erika Musterfrau"])
    live = finished_job(tmp_path, "job-live", data)
    (live / "copy.txt").write_bytes(data)
    finished_job(tmp_path, "job-old", data)
    writing_job(tmp_path, "job-stalled", data)
    make_job(tmp_path, data, PDF_MIME, name="job-queued")
    (tmp_path / "planted").mkdir()
    (tmp_path / "planted" / "input.pdf").write_bytes(data)  # shaped like a job: kept while it is young
    deep_tree(tmp_path / "planted", 1100, data)  # deeper than Python's recursion limit
    # Young: the non-protocol entries of the kept directories go at once (`copy.txt`, the tree `d`).
    assert worker.sweep(tmp_path, live="job-live") == 2
    assert names(tmp_path) == ["job-live", "job-old", "job-queued", "job-stalled", "planted"]
    assert names(tmp_path / "planted") == ["input.pdf"]
    later = time.time() + worker.LEFTOVER_AFTER_S + 1
    assert worker.sweep(tmp_path, live="job-live", now=later) == 4
    assert names(tmp_path) == ["job-live"]
    assert names(live) == ["input.pdf", "request.claimed", "result.json", "text.txt"]
    assert worker.sweep(tmp_path, now=later) == 1 and names(tmp_path) == []  # once it is no longer live


def test_the_loop_sweeps_its_root_before_its_first_job(tmp_path, monkeypatch) -> None:
    data = minimal_pdf(["Erika Musterfrau"])
    job = make_job(tmp_path, data, PDF_MIME, name="job-queued")
    (job / "copy.txt").write_bytes(data)  # a leftover beside a queued job's protocol files
    (tmp_path / "beside.pdf").write_bytes(data)
    seen: list[list[str]] = []
    real_run_job = worker.run_job

    def run_job_seeing(job_dir: Path) -> None:
        seen.append(sorted([*names(tmp_path), *names(job_dir)]))
        real_run_job(job_dir, extractor=mark)

    monkeypatch.setattr(worker, "run_job", run_job_seeing)
    stop = threading.Event()
    loop = threading.Thread(target=serve, args=(tmp_path,), kwargs={"stop": stop}, daemon=True)
    loop.start()
    try:
        wait_for(job / "result.json")
    finally:
        stop.set()
        loop.join(LOOP_WAIT_S)
    assert seen == [["input.pdf", "job-queued", "request.json"]]


def refusing_removal(monkeypatch, refused: str) -> None:
    """`_remove_tree` raises PermissionError for the entry `refused`, as for one docparse may not remove."""
    real = worker._remove_tree

    def remove_tree(dir_fd: int, name: str) -> None:
        if name == refused:
            raise PermissionError(errno.EACCES, "Permission denied", name)
        real(dir_fd, name)

    monkeypatch.setattr(worker, "_remove_tree", remove_tree)


def test_a_sweep_that_fails_after_a_job_ends_the_loop_before_its_next_job(
    tmp_path, monkeypatch, caplog
) -> None:
    """(e) A leftover that cannot be removed ends the loop with `SweepFailed`: the job's answer is out, the
    next job is never claimed, and the log names the error class only, never an entry."""
    caplog.set_level(logging.INFO)
    data = minimal_pdf([SECRET_TEXT])
    first = make_job(tmp_path, data, PDF_MIME, name="job-first")
    os.utime(first / "request.json", (1, 1))  # the oldest request is taken first
    second = make_job(tmp_path, data, PDF_MIME, name="job-second")
    refusing_removal(monkeypatch, "stuck")
    loop, stop, raised = serving(tmp_path, monkeypatch, conftest.plant_stuck)
    loop.join(LOOP_WAIT_S)
    stop.set()
    assert not loop.is_alive(), "the loop went on after a failed sweep"
    assert [type(exc) for exc in raised] == [worker.SweepFailed]
    assert result_of(first)["status"] == "ok"
    assert names(second) == ["input.pdf", "request.json"]  # never claimed
    messages = [r.getMessage() for r in caplog.records if r.name == "docparse"]
    assert "docparse.leftover_not_removed error=PermissionError" in messages
    assert "stuck" not in caplog.text and "Mustermann" not in caplog.text
    assert "stuck" not in str(raised[0])


def test_a_sweep_that_fails_at_start_takes_no_job(tmp_path, monkeypatch) -> None:
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    (tmp_path / "stuck").write_bytes(b"left by an earlier parse")
    refusing_removal(monkeypatch, "stuck")
    loop, stop, raised = serving(tmp_path, monkeypatch, mark)
    loop.join(LOOP_WAIT_S)
    stop.set()
    assert not loop.is_alive(), "the loop went on after a failed sweep"
    assert [type(exc) for exc in raised] == [worker.SweepFailed]
    assert names(job) == ["input.pdf", "request.json"]


MAIN_FAILING_SCRIPT = """
import errno, os, sys
from docparse import worker
def refuse(dir_fd, name):
    raise PermissionError(errno.EACCES, "Permission denied", name)
worker._remove_tree = refuse
worker.main()
"""


def test_main_exits_non_zero_when_the_sweep_fails(tmp_path) -> None:
    """(e) So compose restarts the container, which ends every process of it; a sandbox whose sweep still
    fails is recreated by the operator."""
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    (tmp_path / "stuck").write_bytes(b"left by an earlier parse")
    env = {**os.environ, "JOBHUNTER_PARSE_JOBS_DIR": str(tmp_path), "PYTHONPATH": str(REPO)}
    done = subprocess.run(
        [sys.executable, "-c", MAIN_FAILING_SCRIPT],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=LOOP_WAIT_S,
        check=False,
    )
    assert done.returncode == worker.SWEEP_FAILED_EXIT
    assert "docparse.sweep_failed" in done.stderr and "Traceback" not in done.stderr
    assert "stuck" not in done.stderr
    assert names(job) == ["input.pdf", "request.json"]


SERVE_SCRIPT = "from docparse import worker; worker.main()"


@pytest.mark.skipif(os.geteuid() != 0, reason="only root makes an entry that another uid cannot remove")
def test_as_another_uid_the_loop_opens_up_its_own_leftovers_and_stops_at_one_it_cannot_remove(
    unprivileged_root,
) -> None:
    """Run as nobody, as docparse runs as 10002: a directory of its own uid that it may not enter is opened up
    and removed; a directory root made, with a file inside, cannot be, so `main` exits non-zero at start
    without taking the queued job."""
    root = unprivileged_root
    job = make_job(root, minimal_pdf(["Max Mustermann"]), PDF_MIME)
    locked = job / "locked"
    (locked / "inner").mkdir(parents=True)
    (locked / "inner" / "copy.pdf").write_bytes(b"%PDF-1.4 a copy")
    chown_tree(root)
    (locked / "inner").chmod(0)
    locked.chmod(0o500)
    env = {"JOBHUNTER_PARSE_JOBS_DIR": str(root)}
    with subprocess.Popen(
        [sys.executable, "-c", SERVE_SCRIPT],
        cwd=REPO,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1", **env},
        **as_unprivileged(),
    ) as proc:
        try:
            wait_for(job / "result.json")
        finally:
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(LOOP_WAIT_S) == 0
    assert not locked.exists() and result_of(job)["status"] == "ok"
    stuck = root / "stuck"
    stuck.mkdir()  # root's, 0755: nobody may not remove the file inside it
    (stuck / "copy.pdf").write_bytes(b"%PDF-1.4 a copy")
    queued = make_job(root, minimal_pdf(["Erika Musterfrau"]), PDF_MIME, name="job-queued")
    chown_tree(queued)
    done = run_unprivileged(SERVE_SCRIPT, env=env)
    assert done.returncode == worker.SWEEP_FAILED_EXIT, done.stderr
    assert "docparse.leftover_not_removed error=PermissionError" in done.stderr
    assert names(queued) == ["input.pdf", "request.json"] and names(stuck) == ["copy.pdf"]


def test_main_serves_the_root_of_jobhunter_parse_jobs_dir_and_stops_on_sigterm(tmp_path) -> None:
    assert worker.DEFAULT_ROOT == Path("/data/parse")
    job = make_job(tmp_path, docx_bytes(["Erika Musterfrau"]), DOCX_MIME)
    env = {**os.environ, "JOBHUNTER_PARSE_JOBS_DIR": str(tmp_path), "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen([sys.executable, "-m", "docparse.worker"], cwd=REPO, env=env)
    try:
        wait_for(job / "result.json")
        assert result_of(job)["status"] == "ok"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(LOOP_WAIT_S) == 0
    finally:
        proc.kill()
        proc.wait()
