"""`jobhunter migrate`'s flags (spec §19.4, §19.5 "Monthly restore test"; plan M5 Task 23, Deviation 38).

`--steps` selects step groups (`sequences` = steps 1 and 3, `alembic` = step 2, `replay` = step 7; without
it all seven run), each run in `MIGRATE_ORDER`'s order whatever the flag's; `--database` and `--db-role`
select the DSN of the run (`--db-role restore`: `jobhunter_restore` with the backup role's secrets, as the
restore test runs in the backup container). No test reaches a database: the run's first connection, the
migrate lock's, is recorded and refused. Every secret is a stand-in.
"""

import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest
from jobhunter import cli
from jobhunter.config import AppConfig, Role, load_config
from jobhunter.db import migrate
from jobhunter.db.migrate import MIGRATE_ORDER, STEP_GROUPS, selected_steps
from psycopg.conninfo import conninfo_to_dict

# A stand-in per secret file of the two roles a `migrate` starts as.
STAND_INS: Final = {
    "db_migrator": "pw-migrator-stand-in",
    "db_restore": "pw-restore-stand-in",
    "db_backup": "pw-backup-stand-in",
    "restic_password": "pw-restic-stand-in",
}
ENV: Final = {
    "JOBHUNTER_ENV": "test",
    "JOBHUNTER_DB_HOST": "db.invalid",
    "JOBHUNTER_DB_PORT": "5432",
    "JOBHUNTER_DB_NAME": "jobhunter",
}


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)
    for name, value in STAND_INS.items():
        (directory / name).write_text(value, encoding="utf-8")
        (directory / name).chmod(stat.S_IRUSR)
    return directory


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch, secrets_dir: Path) -> list[Role]:
    """The roles `main` starts as: `startup_or_exit` replaced by a recorder that returns the role's config."""
    roles: list[Role] = []

    def startup(role: Role) -> AppConfig:
        roles.append(role)
        return load_config(role, ENV, secrets_dir=secrets_dir)

    monkeypatch.setattr(cli, "startup_or_exit", startup)
    return roles


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The keyword arguments of each `run_migrate` call `main` makes (the run itself replaced)."""
    calls: list[dict[str, Any]] = []

    def recorder(cfg: AppConfig, **kwargs: Any) -> list[str]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(migrate, "run_migrate", recorder)
    return calls


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The conninfo of each connection the run opens; each is refused, so the run ends at the first."""
    seen: list[str] = []

    async def refuse(conninfo: str, *args: object, **kwargs: object) -> object:
        seen.append(conninfo)
        raise psycopg.OperationalError("refused by the test")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", refuse)
    yield seen


def test_steps_map_to_sequences_alembic_and_replay(
    started: list[Role], run_calls: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert STEP_GROUPS == {
        "sequences": ("reconstruct_and_advance", "advance_again"),
        "alembic": ("alembic",),
        "replay": ("replay",),
    }
    # Every step without a selection; a selection runs its steps in MIGRATE_ORDER's order, not the flag's.
    assert selected_steps(None) == MIGRATE_ORDER
    assert selected_steps(frozenset({"replay", "sequences", "alembic"})) == (
        "reconstruct_and_advance",
        "alembic",
        "advance_again",
        "replay",
    )
    assert selected_steps(frozenset({"replay", "sequences"})) == (
        "reconstruct_and_advance",
        "advance_again",
        "replay",
    )
    assert selected_steps(frozenset({"alembic"})) == ("alembic",)

    assert cli.main(["migrate", "--steps", "replay,sequences"]) == 0
    assert cli.main(["migrate", "--steps", " alembic "]) == 0
    assert cli.main(["migrate"]) == 0
    assert [call["steps"] for call in run_calls] == [
        frozenset({"replay", "sequences"}),
        frozenset({"alembic"}),
        None,
    ]
    for bad in ("sequences,seed", "", "sequences,"):
        with pytest.raises(SystemExit) as refused:
            cli.main(["migrate", "--steps", bad])
        assert refused.value.code == 2
        assert "--steps" in capsys.readouterr().err
    assert len(run_calls) == 3  # a refused flag runs nothing


def test_the_cli_passes_every_flag_to_run_migrate(
    started: list[Role], run_calls: list[dict[str, Any]]
) -> None:
    assert cli.main(["migrate"]) == 0
    assert (
        cli.main(
            [
                "migrate",
                "--database",
                "jobhunter_restore_test",
                "--steps",
                "sequences,alembic,replay",
                "--manifest-dir",
                "/scratch/manifests",
                "--cv-dir",
                "/scratch/cv",
                "--exports-dir",
                "/scratch/exports",
                "--db-role",
                "restore",
            ]
        )
        == 0
    )
    assert run_calls == [
        {
            "steps": None,
            "database": None,
            "manifest_dir": None,
            "cv_dir": None,
            "exports_dir": None,
            "db_role": "migrator",
        },
        {
            "steps": frozenset({"sequences", "alembic", "replay"}),
            "database": "jobhunter_restore_test",
            "manifest_dir": Path("/scratch/manifests"),
            "cv_dir": Path("/scratch/cv"),
            "exports_dir": Path("/scratch/exports"),
            "db_role": "restore",
        },
    ]
    # The restore role's login is one of the backup role's secrets: the restore test's container.
    assert started == ["migrate", "backup"]


@pytest.mark.parametrize(
    ("flags", "role", "user", "password", "dbname"),
    [
        ([], "migrate", "jobhunter_migrator", "pw-migrator-stand-in", "jobhunter"),
        (
            ["--database", "jobhunter_other"],
            "migrate",
            "jobhunter_migrator",
            "pw-migrator-stand-in",
            "jobhunter_other",
        ),
        (
            ["--database", "jobhunter_restore_test", "--db-role", "restore"],
            "backup",
            "jobhunter_restore",
            "pw-restore-stand-in",
            "jobhunter_restore_test",
        ),
    ],
)
def test_database_and_db_role_select_the_dsn(
    started: list[Role],
    connections: list[str],
    flags: list[str],
    role: Role,
    user: str,
    password: str,
    dbname: str,
) -> None:
    with pytest.raises(psycopg.OperationalError, match="refused by the test"):
        cli.main(["migrate", *flags])
    assert started == [role]
    (conninfo,) = connections  # the migrate lock's connection, the run's first
    assert {
        key: value
        for key, value in conninfo_to_dict(conninfo).items()
        if key in ("user", "password", "dbname")
    } == {"user": user, "password": password, "dbname": dbname}


def test_manifests_replay_runs_steps_1_3_and_7(started: list[Role], run_calls: list[dict[str, Any]]) -> None:
    """`jobhunter manifests replay` (§19.4) is `migrate` steps 1, 3 and 7 alone, as the migrator, on the
    configured stream or the one `--manifest-dir` names."""
    assert cli.main(["manifests", "replay"]) == 0
    assert cli.main(["manifests", "replay", "--manifest-dir", "/restored/manifests"]) == 0
    assert run_calls == [
        {"steps": frozenset({"sequences", "replay"}), "manifest_dir": None},
        {"steps": frozenset({"sequences", "replay"}), "manifest_dir": Path("/restored/manifests")},
    ]
    assert selected_steps(run_calls[0]["steps"]) == ("reconstruct_and_advance", "advance_again", "replay")
    assert started == ["migrate", "migrate"]
