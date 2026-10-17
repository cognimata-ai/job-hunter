# tests/unit/test_toolchain.py
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def test_package_version() -> None:
    import jobhunter

    assert re.fullmatch(r"\d+\.\d+\.\d+", jobhunter.__version__)


def test_sqlalchemy_async_extra_installed() -> None:
    import greenlet  # noqa: F401  (sqlalchemy[asyncio] pulls it in; 2.1 does not by default)
    from sqlalchemy.ext.asyncio import create_async_engine

    assert create_async_engine("postgresql+psycopg://u:p@127.0.0.1:1/x").dialect.is_async


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("import anthropic\nanthropic.Anthropic()\n", "TID251"),
        ("from langchain_anthropic import ChatAnthropic\n", "TID251"),
        ("def f() -> None:\n    try:\n        pass\n    except Exception:\n        pass\n", "BLE001"),
        ("import datetime\ndatetime.datetime.now()\n", "DTZ005"),
        ("from psycopg.errors import UniqueViolation\n", "TID251"),
    ],
)
def test_ruff_rules_fire(tmp_path: Path, source: str, rule: str) -> None:
    target = tmp_path / "probe.py"
    target.write_text(source)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--config",
            str(REPO / "ruff.toml"),
            "--no-cache",
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and rule in result.stdout
