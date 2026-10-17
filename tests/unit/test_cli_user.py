import getpass
import io
import subprocess
import sys
import warnings

import pytest
from jobhunter import cli


def test_stdin_password_is_one_line_and_loses_only_its_newline() -> None:
    assert cli._read_stdin_password(io.BytesIO(b" a b \nsecond line\n")) == " a b "
    assert cli._read_stdin_password(io.BytesIO(b"no newline at the end")) == "no newline at the end"
    assert cli._read_stdin_password(io.BytesIO(b"tab\t \n")) == "tab\t "
    assert cli._read_stdin_password(io.BytesIO("Ölfass\r\n".encode())) == "Ölfass"  # a CRLF is one newline
    assert cli._read_stdin_password(io.BytesIO(b"\n")) == ""  # the policy, not the reader, rejects it


def test_stdin_password_missing_or_not_utf8_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli._read_stdin_password(io.BytesIO(b"")) is None
    assert "no password" in capsys.readouterr().err
    assert cli._read_stdin_password(io.BytesIO(b"\xff\xfe\xfd\n")) is None
    assert "UTF-8" in capsys.readouterr().err


def test_a_prompted_password_is_asked_twice_and_must_match(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts: list[str] = []
    answers = iter(["Tannenbaum-Ölfass-7", "Tannenbaum-Ölfass-7"])

    def ask(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(getpass, "getpass", ask)
    assert cli._prompt_new_password() == "Tannenbaum-Ölfass-7"
    assert len(prompts) == 2 and prompts[0] != prompts[1]

    answers = iter(["first-secret-AAAA", "second-secret-BBBB"])
    assert cli._prompt_new_password() is None
    shown = capsys.readouterr()
    assert "do not match" in shown.err and "secret" not in shown.out + shown.err


def test_a_prompt_that_cannot_hide_the_input_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_terminal(prompt: str = "") -> str:
        # What getpass does when it cannot switch the echo off: it warns, then reads with the echo on.
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2)
        raise AssertionError("the password must not be read with the echo on")

    monkeypatch.setattr(getpass, "getpass", no_terminal)
    assert cli._prompt_new_password() is None
    assert "--password-stdin" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv", [["user"], ["user", "create"], ["user", "totp"], ["user", "totp", "bogus"], ["user", "bogus"]]
)
def test_incomplete_user_commands_are_usage_errors(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 2


def test_importing_the_cli_loads_none_of_the_user_command_libraries() -> None:
    """`startup-check` and the health probes never hash, encrypt or draw a QR code."""
    code = (
        "import sys, jobhunter.cli\n"
        "heavy = ('argon2', 'pyotp', 'segno', 'cryptography', 'sqlalchemy', 'jobhunter.auth')\n"
        "loaded = [name for name in heavy if name in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
