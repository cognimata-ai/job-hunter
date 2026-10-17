import subprocess
import sys


def test_readiness_does_not_import_the_worker_singleton() -> None:
    """`observability.health` shares the heartbeat threshold through a neutral module, not `worker.lock`."""
    code = (
        "import sys, jobhunter.observability.health\n"
        "assert 'jobhunter.worker.lock' not in sys.modules, 'health imports worker.lock'\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
