import subprocess
import sys

HEAVY = ("procrastinate", "langgraph", "alembic")


def test_importing_the_cli_does_not_load_the_migrate_machinery() -> None:
    """`startup-check` (every container's healthcheck path) must not pay for procrastinate and langgraph."""
    code = (
        "import sys, jobhunter.cli\n"
        f"loaded = [name for name in {HEAVY!r} if name in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
