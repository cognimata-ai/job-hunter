"""The docparse job runner and loop (spec §12.2; Task 8): the claim, the rlimited spawn child, the atomic
results and the loop that logs only job ids and codes.

Run with `uv run --group docparse pytest -W error -m docparse tests/docparse`. The extractors that run in the
child live in `tests.docparse.conftest`, which the spawn child imports by module path.
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import docparse.worker as worker
from docparse.worker import Limits, claim, run_job, serve
from tests.docparse import conftest
from tests.docparse.conftest import (
    MARKED_TEXT,
    SECRET_TEXT,
    burn,
    die,
    explode,
    flood,
    hog,
    make_job,
    mark,
    report_inherited,
    result_of,
    sleeper,
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
