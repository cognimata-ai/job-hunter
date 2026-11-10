"""The `backup` and `restore` startup roles (plan M5 Task 10, R4, Deviation 37): their secrets, the S3 check
of an `s3:` repository (`restic_s3_credentials`), the DB-role selector of the DSN, and the backup package's
import boundary (O1: the backup container never writes an alert; the worker derives them from its tables).
"""

import ast
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
from jobhunter.config import ROLE_SECRETS, Role, StartupCheckFailed, load_config, run_startup_assertions

ENV: Final = {
    "JOBHUNTER_ENV": "test",
    "JOBHUNTER_DB_HOST": "h",
    "JOBHUNTER_DB_PORT": "5432",
    "JOBHUNTER_DB_NAME": "jobhunter",
}
BACKUP_SECRETS: Final = {"db_backup": b"bak pw", "db_restore": b"rst", "restic_password": b"r-pw"}
RESTORE_SECRETS: Final = {"db_migrator": b"mig", "restic_password": b"r-pw"}
S3_SECRETS: Final = {"s3_access_key_id": b"AKIDNOTAKEY", "s3_secret_access_key": b"not-a-secret"}
S3_REPOSITORY: Final = "s3:https://s3.example.test/jobhunter-backups"


def write_secrets(directory: Path, files: dict[str, bytes]) -> Path:
    directory.mkdir()
    for name, value in files.items():
        (directory / name).write_bytes(value)
        (directory / name).chmod(0o440)
    return directory


def refusal(role: Role, secrets_dir: Path, **env: str) -> StartupCheckFailed | None:
    try:
        run_startup_assertions(load_config(role, {**ENV, **env}, secrets_dir))
    except StartupCheckFailed as failed:
        return failed
    return None


def test_backup_and_restore_roles_require_their_secrets(tmp_path: Path) -> None:
    s3 = frozenset(S3_SECRETS)
    assert ROLE_SECRETS["backup"].required == {"db_backup", "db_restore", "restic_password"}
    assert ROLE_SECRETS["backup"].optional == s3
    assert ROLE_SECRETS["restore"].required == {"db_migrator", "restic_password"}
    assert ROLE_SECRETS["restore"].optional == s3

    roles: tuple[tuple[Role, dict[str, bytes]], ...] = (
        ("backup", BACKUP_SECRETS),
        ("restore", RESTORE_SECRETS),
    )
    for role, secrets in roles:
        assert refusal(role, write_secrets(tmp_path / role, secrets)) is None
        for name in secrets:
            without = write_secrets(
                tmp_path / f"{role}-{name}", {k: v for k, v in secrets.items() if k != name}
            )
            failed = refusal(role, without)
            assert failed is not None and (failed.check, failed.detail) == ("secrets_present", name)

    # `startup-check` names its role, and both are among its choices.
    from jobhunter.cli import main

    for role in ("backup", "restore"):
        with pytest.raises(SystemExit) as exited:
            main(["startup-check", "--role", role])
        assert exited.value.code == 78  # this process has no secret files: refused, not an unknown role


@pytest.mark.parametrize("role", ["backup", "restore"])
def test_an_s3_repository_needs_both_s3_secrets(tmp_path: Path, role: Role) -> None:
    base = BACKUP_SECRETS if role == "backup" else RESTORE_SECRETS
    for index, present in enumerate(([], ["s3_access_key_id"], ["s3_secret_access_key"])):
        directory = write_secrets(tmp_path / f"s3-{index}", {**base, **{n: S3_SECRETS[n] for n in present}})
        failed = refusal(role, directory, RESTIC_REPOSITORY=S3_REPOSITORY)
        missing = ", ".join(sorted(set(S3_SECRETS) - set(present)))
        assert failed is not None
        assert (failed.check, failed.detail) == (
            "restic_s3_credentials",
            f"an s3: repository needs {missing}",
        )
        assert "AKIDNOTAKEY" not in str(failed) and "not-a-secret" not in str(failed)

    both = write_secrets(tmp_path / "both", {**base, **S3_SECRETS})
    assert refusal(role, both, RESTIC_REPOSITORY=S3_REPOSITORY) is None
    # A local repository (the dev shipper's) needs neither; nor does a role without a repository.
    assert (
        refusal(role, write_secrets(tmp_path / "local", base), RESTIC_REPOSITORY="/data/restic-repo") is None
    )
    assert refusal(role, write_secrets(tmp_path / "none", base)) is None


def test_dsn_selects_the_db_role_and_database(tmp_path: Path) -> None:
    secrets = {
        "db_app": b"app pw",
        "db_migrator": b"mig/pw",
        "db_backup": b"bak@pw",
        "db_restore": b"rst:pw",
        "restic_password": b"r",
        "session_secret": b"s",
        "data_key": b"d",
    }
    directory = write_secrets(tmp_path / "s", secrets)
    login = {
        "app": "jobhunter_app:app%20pw",
        "migrator": "jobhunter_migrator:mig%2Fpw",
        "backup": "jobhunter_backup:bak%40pw",
        "restore": "jobhunter_restore:rst%3Apw",
    }
    defaults: dict[Role, str] = {
        "api": "app",
        "worker": "app",
        "migrate": "migrator",
        "backup": "backup",
        "restore": "migrator",
    }
    for role, db_role in defaults.items():
        cfg = load_config(role, ENV, directory)
        assert cfg.dsn("psycopg") == f"postgresql://{login[db_role]}@h:5432/jobhunter"
        assert cfg.dsn("sqlalchemy") == f"postgresql+psycopg://{login[db_role]}@h:5432/jobhunter"

    backup = load_config("backup", ENV, directory)
    assert backup.dsn("psycopg", db_role="restore") == f"postgresql://{login['restore']}@h:5432/jobhunter"
    assert (
        backup.dsn("psycopg", db_role="restore", database="jobhunter_restoretest 1")
        == f"postgresql://{login['restore']}@h:5432/jobhunter_restoretest%201"
    )
    restore = load_config("restore", ENV, directory)
    assert restore.dsn("sqlalchemy", database="postgres") == (
        f"postgresql+psycopg://{login['migrator']}@h:5432/postgres"
    )


def test_a_foreign_secret_is_refused_for_backup(tmp_path: Path) -> None:
    mounted = write_secrets(tmp_path / "s", {**BACKUP_SECRETS, "session_secret": b"s" * 44})
    failed = refusal("backup", mounted)
    assert failed is not None and (failed.check, failed.detail) == (
        "foreign_secret_mounted",
        "session_secret",
    )

    worker_key = write_secrets(tmp_path / "w", {**RESTORE_SECRETS, "db_app": b"app"})
    failed = refusal("restore", worker_key)
    assert failed is not None and (failed.check, failed.detail) == ("foreign_secret_mounted", "db_app")


def test_the_backup_package_never_imports_observability_alerts() -> None:
    code = (
        "import sys\n"
        "import jobhunter.backup, jobhunter.backup.runner, jobhunter.backup.restic\n"
        "import jobhunter.backup.shipper\n"
        "import jobhunter.cli_backup\n"
        "assert 'jobhunter.backup.shipper' in sys.modules\n"
        "assert 'jobhunter.observability.alerts' not in sys.modules, 'observability.alerts was imported'\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr

    # Nor does any module of the package name it in an import, even one inside a function.
    package = Path(__file__).resolve().parents[2] / "backend" / "src" / "jobhunter" / "backup"
    modules = sorted(package.glob("*.py"))
    assert {module.name for module in modules} >= {"__init__.py", "runner.py", "restic.py", "shipper.py"}
    for module in modules:
        assert "jobhunter.observability.alerts" not in imported_names(module), module.name


def imported_names(module: Path) -> set[str]:
    """Every module an import statement of `module` names, `from x import y` as both `x` and `x.y`."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names
