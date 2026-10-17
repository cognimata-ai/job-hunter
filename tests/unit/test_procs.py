"""`ProcessGroup` teardown: no child, and no descendant of one, outlives the group."""

import os
import signal
import sys
import threading
from pathlib import Path

from tests.support.procs import ProcessGroup, wait_until

# The leader starts a grandchild that inherits its pipes and process group, reports its pid, and exits.
_LEADER = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "print(child.pid, flush=True)\n"
)


def _gone(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except FileNotFoundError:
        return True
    return stat.rpartition(")")[2].split()[0] == "Z"  # dead, waiting only to be reaped by its new parent


def test_close_kills_the_group_after_the_leader_was_reaped() -> None:
    group = ProcessGroup()
    leader = group.spawn([sys.executable, "-c", _LEADER], os.environ)
    wait_until(lambda: leader.output().strip().isdigit(), 20)
    grandchild = int(leader.output())
    try:
        assert leader.exit_within(20) == 0  # the leader is reaped; the grandchild still holds the pipes
        closer = threading.Thread(target=group.close, daemon=True)
        closer.start()
        closer.join(15)
        assert not closer.is_alive(), "close() stalled on the grandchild's pipes"
        assert _gone(grandchild)
    finally:
        if not _gone(grandchild):
            os.kill(grandchild, signal.SIGKILL)
