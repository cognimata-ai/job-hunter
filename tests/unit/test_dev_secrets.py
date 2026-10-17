"""`scripts/dev-secrets.sh` (Task 18) without root: PATH shims stand in for `chgrp` and, for a race, `ln`.

The stack test checks the real group 10010 and the modes as root; these check the script's own logic.
"""

import base64
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev-secrets.sh"
B64_SECRETS = ("session_secret", "data_key")
PASSWORDS = ("pg_bootstrap", "db_app", "db_migrator", "db_backup", "db_restore")
NAMES = B64_SECRETS + PASSWORDS

# A chgrp 10010 that succeeds, as it does for root.
CHGRP_OK = "#!/bin/sh\nexit 0\n"
# A chgrp that refuses, as it does for a user outside group 10010: for everything, or for regular files only.
CHGRP_REFUSED = '#!/bin/sh\necho "chgrp: Operation not permitted" >&2\nexit 1\n'
CHGRP_REFUSED_FOR_FILES = (
    '#!/bin/sh\nfor target; do :; done\nif [ -f "$target" ]; then echo "chgrp: refused" >&2; exit 1; fi\n'
    "exit 0\n"
)
# Another run creates the secret between the existence check and the link: the shim writes the name itself,
# then fails like `ln` does when the name exists.
LN_RACED = '#!/bin/sh\nfor target; do :; done\nprintf theirs > "$target"\nexit 1\n'
SUDO_HINT = (
    "dev-secrets: cannot set group 10010 on {}; run as root (sudo) — the app runs as uid 10001 with group "
    "10010 and cannot read the file otherwise\n"
)


def run_script(tmp_path: Path, shims: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in shims.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        [str(SCRIPT), str(tmp_path / "dev")], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def test_each_secret_is_created_once_as_0440_and_only_its_name_is_printed(tmp_path: Path) -> None:
    first = run_script(tmp_path, {"chgrp": CHGRP_OK})
    assert (first.returncode, first.stderr) == (0, "")
    assert first.stdout.splitlines() == [f"dev-secrets: created {name}" for name in NAMES]
    secrets_dir = tmp_path / "dev"
    assert sorted(os.listdir(secrets_dir)) == sorted(NAMES)  # no temporary file left behind
    assert secrets_dir.stat().st_mode & 0o777 == 0o750
    values = {name: (secrets_dir / name).read_text() for name in NAMES}
    for name, value in values.items():
        assert (secrets_dir / name).stat().st_mode & 0o777 == 0o440
        assert value.strip() not in first.stdout
    assert all(len(base64.b64decode(values[name].strip(), validate=True)) == 32 for name in B64_SECRETS)
    assert all(len(values[name].strip()) == 48 and int(values[name], 16) >= 0 for name in PASSWORDS)

    second = run_script(tmp_path, {"chgrp": CHGRP_OK})
    assert (second.returncode, second.stdout, second.stderr) == (0, "", "")
    assert {name: (secrets_dir / name).read_text() for name in NAMES} == values


def test_a_secret_created_meanwhile_is_kept_and_never_reported_as_created(tmp_path: Path) -> None:
    done = run_script(tmp_path, {"chgrp": CHGRP_OK, "ln": LN_RACED})
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout.splitlines() == [f"dev-secrets: kept {name}" for name in NAMES]
    secrets_dir = tmp_path / "dev"
    assert sorted(os.listdir(secrets_dir)) == sorted(NAMES)
    assert all((secrets_dir / name).read_text() == "theirs" for name in NAMES)


@pytest.mark.parametrize(
    ("chgrp", "refused"), [(CHGRP_REFUSED, ""), (CHGRP_REFUSED_FOR_FILES, "session_secret")]
)
def test_a_group_that_cannot_be_set_stops_with_the_sudo_hint_and_leaves_no_secret(
    tmp_path: Path, chgrp: str, refused: str
) -> None:
    done = run_script(tmp_path, {"chgrp": chgrp})
    target = tmp_path / "dev" / refused if refused else tmp_path / "dev"
    assert (done.returncode, done.stdout, done.stderr) == (1, "", SUDO_HINT.format(target))
    assert os.listdir(tmp_path / "dev") == []
