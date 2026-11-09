"""docparse's leftover marker (plan M5 Task 3, M2 Carry-forward 3, Deviation 7): before a failed sweep's exit
70, docparse writes `<parse root>/.docparse-leftover` (empty, 0660); a clean start sweep removes it. The
worker's `metrics_refresh` raises `docparse.leftover` while it is there, so it reads the name docparse writes.
"""

import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Final

from jobhunter.profile import parse_client
from jobhunter.worker import reconcilers

import docparse.worker as worker

REPO: Final = Path(__file__).resolve().parents[2]
# `main()` as compose runs it, with an entry of the root that its sweep cannot remove, under a umask that
# would leave a new file 0600: only its `fchmod` makes the marker 0660.
FAILING_SWEEP: Final = """
import errno, os
os.umask(0o077)
from docparse import worker
def refuse(dir_fd, name):
    if name == "stuck":
        raise PermissionError(errno.EACCES, "Permission denied", name)
    real(dir_fd, name)
real = worker._remove_tree
worker._remove_tree = refuse
worker.main()
"""


def test_the_worker_reads_the_name_docparse_writes() -> None:
    # docparse is a package of its own (the app image does not contain it), so the backend repeats the name
    # once, in its half of the file protocol; the reconciler reads it from there
    assert worker.LEFTOVER_MARKER == parse_client.LEFTOVER_MARKER == ".docparse-leftover"
    assert reconcilers.DOCPARSE_LEFTOVER_MARKER is parse_client.LEFTOVER_MARKER


def test_a_failed_sweep_writes_the_marker_before_exit_70(tmp_path: Path) -> None:
    (tmp_path / "stuck").write_bytes(b"left by an earlier parse")
    env = {**os.environ, "JOBHUNTER_PARSE_JOBS_DIR": str(tmp_path), "PYTHONPATH": str(REPO)}
    done = subprocess.run(
        [sys.executable, "-c", FAILING_SWEEP],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == worker.SWEEP_FAILED_EXIT == 70, done.stderr
    marker = tmp_path / worker.LEFTOVER_MARKER
    st = marker.lstat()
    assert stat.S_ISREG(st.st_mode) and st.st_size == 0
    assert stat.S_IMODE(st.st_mode) == 0o660  # whatever the umask: the worker's group reads it
    assert "docparse.sweep_failed" in done.stderr and "Traceback" not in done.stderr


def test_a_clean_sweep_removes_the_marker(tmp_path: Path) -> None:
    marker = tmp_path / worker.LEFTOVER_MARKER
    marker.touch(mode=0o660)
    stop = threading.Event()
    stop.set()  # the start sweep only: no job is served
    worker.serve(tmp_path, stop=stop)
    assert not marker.exists()


def test_clear_leftover_marker_removes_the_marker_itself_and_nothing_else(tmp_path: Path) -> None:
    marker = tmp_path / worker.LEFTOVER_MARKER
    marker.touch(mode=0o660)
    (tmp_path / "job-1").mkdir()
    worker.clear_leftover_marker(tmp_path)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["job-1"]
    worker.clear_leftover_marker(tmp_path)  # none: nothing to do, no error
    target = tmp_path / "elsewhere"
    target.write_bytes(b"")
    marker.symlink_to(target)  # a symlink of that name is removed, never its target
    worker.clear_leftover_marker(tmp_path)
    assert not marker.is_symlink() and target.exists()
