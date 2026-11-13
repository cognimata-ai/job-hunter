"""The operator file (spec §19.2, §20.6; M5 follow-ups Task F1, Ruling FU-1): the privacy statement's five
operator values, which the repository never holds.

- `privacy.operator.load` reads a JSON object of exactly the five keys, each a string that is not blank, of
  at most 200 characters, with no control character: the details. No file at the path is `None`; anything
  else is `OperatorFileInvalid`, whose reason names the problem and never a value or an unknown key's name:
  a file that cannot be read, that is no regular file or too large, that is not UTF-8 or not JSON, not an
  object, with a key missing, another key or a key twice, a value of another type, or a blank, overlong or
  control-character value.
- `at_start`, the api's one read: outside prod a missing or invalid file is no details (the statement keeps
  its markers), the invalid one logged; in prod either raises.
- The startup check `operator_file` refuses a prod api without a valid file, naming the path and the problem,
  never a value; it passes a prod api with one, the api outside prod without one, and every other role; the
  CLI exits 78.
- The compose mount: `deploy/compose.yaml` mounts `/etc/jobhunter/operator.json` read-only into the api only,
  at the setting's default, and never makes a directory in its place; the test stack mounts
  `deploy/test/operator.json` there instead, whose values are fictional and valid.
- No value reaches a log line, whatever the outcome (a caplog probe; no value has a shape the redaction
  masks).
"""

import json
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from jobhunter.config import (
    DEFAULT_OPERATOR_FILE,
    ENV_NAMES,
    ENV_PREFIXES,
    StartupCheckFailed,
    load_config,
    run_startup_assertions,
    startup_or_exit,
)
from jobhunter.observability.redact import mask_text
from jobhunter.privacy import operator
from jobhunter.privacy.operator import FIELDS, MAX_BYTES, MAX_CHARS, OperatorDetails, OperatorFileInvalid

from tests.unit.test_compose_prod import DEPLOY, prod_config, pt_config
from tests.unit.test_config import API_SECRETS, BASE_ENV, WORKER_SECRETS, write_secrets

# Distinctive values, none e-mail- or phone-shaped: the log redaction (which runs before a record is captured)
# masks those, so a probe of such a value would pass whether it leaked or not.
VALUES: Final[Mapping[str, str]] = {
    "operator": "Quirinus Zappelmeier",
    "contact": "Postfach am Kuckucksweg, Zappelstadt",
    "controller": "Zappelmeier Datenhaltung GmbH",
    "vps_provider": "Wolkenkuckucksheim Hosting",
    "s3_provider": "Eimerweise Objektspeicher AG",
}
PROD_API_ENV: Final = {**BASE_ENV, "JOBHUNTER_ENV": "prod"}
TEST_OPERATOR_FILE: Final = DEPLOY / "test/operator.json"
TARGET: Final = "/run/config/operator.json"
HOST_FILE: Final = "/etc/jobhunter/operator.json"


def document(**changes: object) -> dict[str, object]:
    """`VALUES` with `changes`; a change to `None` removes that key."""
    merged: dict[str, object] = {**VALUES, **changes}
    return {key: value for key, value in merged.items() if value is not None}


def write(path: Path, content: bytes | Mapping[str, object]) -> Path:
    path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
    return path


def problem_of(path: Path) -> str:
    with pytest.raises(OperatorFileInvalid) as exc:
        operator.load(path)
    reason = str(exc.value)
    assert reason == exc.value.reason
    assert unchained(exc.value), "no parser error, which holds the text"
    return reason


def unchained(exc: BaseException) -> bool:
    """`exc` carries no other exception: neither a cause nor a context a traceback would print."""
    return exc.__cause__ is None and (exc.__context__ is None or exc.__suppress_context__)


def assert_no_value(text: str) -> None:
    assert [name for name, value in VALUES.items() if value in text] == [], text


def events(records: list[logging.LogRecord]) -> list[dict[str, Any]]:
    """The structlog events among `records` (`tests.support.logs`)."""
    return [record.msg for record in records if isinstance(record.msg, dict)]


def json_object(*pairs: str) -> bytes:
    """A JSON object of the five keys, each with a short value, but for `pairs` (`"key": <JSON>` texts) in
    their place."""
    given = {pair.split(":", 1)[0].strip().strip('"') for pair in pairs}
    rest = [f'"{key}": "x"' for key in FIELDS if key not in given]
    return ("{" + ", ".join([*pairs, *rest]) + "}").encode()


OTHER_KEYS: Final = "other than operator, contact, controller, vps_provider, s3_provider"


# --- the loader ---------------------------------------------------------------------------------------------


def test_a_valid_file_loads_its_five_values(tmp_path: Path) -> None:
    longest = "Ä" * MAX_CHARS  # characters, not bytes: 400 bytes of UTF-8
    path = write(tmp_path / "operator.json", document(controller=longest, s3_provider="  Eimer  "))
    details = operator.load(path)
    assert details == OperatorDetails(**{**VALUES, "controller": longest, "s3_provider": "  Eimer  "})
    assert FIELDS == ("operator", "contact", "controller", "vps_provider", "s3_provider")
    assert MAX_CHARS == 200
    # a details object that reaches a log line or a traceback shows no value
    assert_no_value(repr(details))
    assert "Ä" not in repr(details)


def test_pretty_printed_json_and_a_final_newline_are_fine(tmp_path: Path) -> None:
    path = write(
        tmp_path / "operator.json", (json.dumps(VALUES, indent=2, ensure_ascii=False) + "\n").encode()
    )
    assert operator.load(path) == OperatorDetails(**VALUES)


@pytest.mark.parametrize("where", ["absent.json", "no-such-dir/operator.json"])
def test_no_file_is_none(tmp_path: Path, where: str) -> None:
    assert operator.load(tmp_path / where) is None


def test_a_path_under_a_file_is_none(tmp_path: Path) -> None:
    write(tmp_path / "plain", b"{}")
    assert operator.load(tmp_path / "plain/operator.json") is None


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b'{"operator": "Quirinus Zappelmeier", ', "is not valid JSON (line 1, column 38)"),
        (b"", "is not valid JSON (line 1, column 1)"),
        (b"\xff\xfe{}", "is not valid UTF-8"),
        ("﻿".encode() + json.dumps(VALUES).encode(), "is not valid JSON (line 1, column 1)"),
        (b"[" * 10000, "is nested too deeply"),
        (b"[]", "must be a JSON object"),
        (b'"Quirinus Zappelmeier"', "must be a JSON object"),
        (b"null", "must be a JSON object"),
        (document(vps_provider=None), "lacks vps_provider"),
        (document(operator=None, contact=None), "lacks operator, contact"),
        (document(phone="+49 30 1234567"), f"has 1 key {OTHER_KEYS}"),
        (document(**{"Quirinus Zappelmeier": "x", "vps-provider": "y"}), f"has 2 keys {OTHER_KEYS}"),
        (json_object('"operator": "Quirinus Zappelmeier"', '"operator": "Q"'), "has operator more than once"),
        (json_object('"Quirinus Zappelmeier": 1', '"Quirinus Zappelmeier": 2'), "has a key more than once"),
        (document(operator=1), "operator must be a string"),
        (document(contact=["Postfach am Kuckucksweg"]), "contact must be a string"),
        (document(controller={"name": "Zappelmeier Datenhaltung GmbH"}), "controller must be a string"),
        (document(vps_provider=True), "vps_provider must be a string"),
        (json_object('"operator": null'), "operator must be a string"),
        (json_object('"operator": NaN'), "operator must be a string"),
        (document(operator=""), "operator must not be empty"),
        (document(s3_provider="   "), "s3_provider must not be empty"),
        (document(s3_provider=" \t "), "s3_provider must not be empty"),  # blank, before its characters
        (document(contact="q" * (MAX_CHARS + 1)), f"contact must be at most {MAX_CHARS} characters"),
        (document(operator="Quirinus\nZappelmeier"), "operator must contain no control character"),
        (document(operator="Quirinus\x00Zappelmeier"), "operator must contain no control character"),
        (document(operator="Quirinus\x1bZappelmeier"), "operator must contain no control character"),
        (document(operator="Quirinus\x7fZappelmeier"), "operator must contain no control character"),
        (document(operator="Quirinus\x85Zappelmeier"), "operator must contain no control character"),  # C1
        (document(operator="Quirinus\u2028Zappelmeier"), "operator must contain no control character"),
        (document(operator="Quirinus\u2029Zappelmeier"), "operator must contain no control character"),
        (json_object('"operator": "\\ud800"'), "operator must be valid Unicode text"),
    ],
    ids=lambda value: value if isinstance(value, str) and len(value) < 60 else None,
)
def test_an_invalid_file_is_refused_naming_the_problem_never_a_value(
    tmp_path: Path, content: bytes | Mapping[str, object], reason: str
) -> None:
    path = write(tmp_path / "operator.json", content)
    found = problem_of(path)
    assert found == reason
    assert_no_value(found)
    assert "Quirinus" not in found and "phone" not in found and "vps-provider" not in found


def test_a_file_too_large_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path / "operator.json", json.dumps(VALUES).encode() + b" " * MAX_BYTES)
    assert problem_of(path) == f"is larger than {MAX_BYTES} bytes"
    assert MAX_BYTES == 16384
    # the largest valid file, every value at its limit and escaped, fits with room to spare
    escaped = json.dumps({key: "ä" * MAX_CHARS for key in FIELDS}, indent=4).encode()
    assert len(escaped) < MAX_BYTES // 2


def test_no_regular_file_in_its_place_is_refused(tmp_path: Path) -> None:
    """A directory (what a bind mount's short syntax would make of a missing host file), and a FIFO, which is
    refused at once, not waited on."""
    (tmp_path / "operator.json").mkdir()
    assert problem_of(tmp_path / "operator.json") == "is not a regular file"
    os.mkfifo(tmp_path / "fifo")
    assert problem_of(tmp_path / "fifo") == "is not a regular file"


def test_a_file_that_cannot_be_opened_is_refused(tmp_path: Path) -> None:
    (tmp_path / "loop").symlink_to(tmp_path / "loop")  # ELOOP, whoever runs it
    assert problem_of(tmp_path / "loop") == "cannot be read"
    if os.geteuid() != 0:  # root reads every file
        path = write(tmp_path / "operator.json", VALUES)
        path.chmod(0o000)
        assert problem_of(path) == "cannot be read"


def test_at_start_outside_prod_a_missing_or_invalid_file_is_none(tmp_path: Path) -> None:
    valid = write(tmp_path / "valid.json", VALUES)
    invalid = write(tmp_path / "invalid.json", document(contact=None))
    assert operator.at_start(valid, prod=False) == OperatorDetails(**VALUES)
    assert operator.at_start(tmp_path / "absent.json", prod=False) is None
    assert operator.at_start(invalid, prod=False) is None
    assert operator.at_start(valid, prod=True) == OperatorDetails(**VALUES)
    with pytest.raises(OperatorFileInvalid, match=r"^is missing$"):
        operator.at_start(tmp_path / "absent.json", prod=True)
    with pytest.raises(OperatorFileInvalid, match=r"^lacks contact$"):
        operator.at_start(invalid, prod=True)


# --- the startup check --------------------------------------------------------------------------------------


def api_config(tmp_path: Path, env: Mapping[str, str], operator_file: Path | None) -> Any:
    extra = {} if operator_file is None else {"JOBHUNTER_OPERATOR_FILE": str(operator_file)}
    return load_config("api", {**env, **extra}, write_secrets(tmp_path / "s", **API_SECRETS))


def test_the_setting_defaults_to_the_mount_target(tmp_path: Path) -> None:
    assert Path(TARGET) == DEFAULT_OPERATOR_FILE
    assert api_config(tmp_path, BASE_ENV, None).operator_file == DEFAULT_OPERATOR_FILE
    given = tmp_path / "elsewhere.json"
    assert api_config(tmp_path, BASE_ENV, given).operator_file == given
    empty = load_config(
        "api", {**BASE_ENV, "JOBHUNTER_OPERATOR_FILE": ""}, write_secrets(tmp_path / "s", **API_SECRETS)
    )
    assert empty.operator_file == DEFAULT_OPERATOR_FILE  # empty is unset


def test_a_prod_api_with_a_valid_file_passes(tmp_path: Path) -> None:
    run_startup_assertions(api_config(tmp_path, PROD_API_ENV, write(tmp_path / "operator.json", VALUES)))


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (None, "is missing"),
        (b"{", "is not valid JSON (line 1, column 2)"),
        (document(s3_provider=None, backup_provider="Eimerweise Objektspeicher AG"), "lacks s3_provider"),
        (document(contact="q" * 201), "contact must be at most 200 characters"),
        (document(controller="Zappelmeier\tDatenhaltung"), "controller must contain no control character"),
    ],
)
def test_a_prod_api_without_a_valid_file_does_not_start(
    tmp_path: Path, content: bytes | Mapping[str, object] | None, problem: str
) -> None:
    path = tmp_path / "operator.json"
    if content is not None:
        write(path, content)
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(api_config(tmp_path, PROD_API_ENV, path))
    assert (exc.value.check, exc.value.detail) == ("operator_file", f"the operator file {path} {problem}")
    assert unchained(exc.value)
    assert_no_value(str(exc.value))


def test_outside_prod_and_for_other_roles_the_file_is_optional(tmp_path: Path) -> None:
    absent = tmp_path / "absent.json"
    invalid = write(tmp_path / "invalid.json", b"not json")
    for env_name in ("dev", "test"):
        env = {**BASE_ENV, "JOBHUNTER_ENV": env_name}
        for path in (None, absent, invalid):
            run_startup_assertions(api_config(tmp_path, env, path))
    # the worker in prod reads no operator file (healthchecks and ntfy are its own prod checks)
    worker_env = {
        **PROD_API_ENV,
        "NTFY_TOPIC_URL": "https://ntfy.example/jh-topic",
        "JOBHUNTER_OPERATOR_FILE": str(absent),
    }
    secrets = {
        **WORKER_SECRETS,
        "healthchecks_ping_url": b"https://hc-ping.example/00000000-0000-4000-8000-000000000001",
        "healthchecks_api_key": b"hc-key",
    }
    run_startup_assertions(load_config("worker", worker_env, write_secrets(tmp_path / "w", **secrets)))


def test_the_check_comes_after_the_public_origin(tmp_path: Path) -> None:
    env = {**PROD_API_ENV, "JOBHUNTER_PUBLIC_ORIGIN": "http://testserver"}
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(api_config(tmp_path, env, tmp_path / "absent.json"))
    assert exc.value.check == "public_origin"


def app_free_environ() -> dict[str, str]:
    """This process's environment without a variable the application reads (`config.ENV_PREFIXES`,
    `ENV_NAMES`): a prod check of this machine's own variables would stop the run before the operator file."""
    return {k: v for k, v in os.environ.items() if not k.startswith(ENV_PREFIXES) and k not in ENV_NAMES}


def test_the_cli_exits_78_naming_the_path_and_the_problem_never_a_value(tmp_path: Path) -> None:
    """Run in `tmp_path`, with the relative path the log line keeps whole: the redaction masks any long run
    of path characters (40 or more) as a key, which the mount target is not."""
    write(tmp_path / "operator.json", document(operator="Quirinus\x07Zappelmeier"))
    secrets = write_secrets(tmp_path / "s", **API_SECRETS)
    env = {
        **app_free_environ(),
        **PROD_API_ENV,
        "JOBHUNTER_SECRETS_DIR": str(secrets),
        "JOBHUNTER_OPERATOR_FILE": "operator.json",
    }
    done = subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", "startup-check", "--role", "api"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 78, done.stdout + done.stderr
    (line,) = done.stdout.splitlines()
    event = json.loads(line)
    assert (event["event"], event["check"], event["role"]) == ("startup_check_failed", "operator_file", "api")
    assert event["detail"] == "the operator file operator.json operator must contain no control character"
    assert_no_value(done.stdout + done.stderr)
    assert "Quirinus" not in done.stdout + done.stderr
    # the mount target is no key to the redaction: in prod the line names it whole
    assert mask_text(f"the operator file {DEFAULT_OPERATOR_FILE} is missing").endswith(f"{TARGET} is missing")


# --- no value in a log line ---------------------------------------------------------------------------------


def test_no_value_reaches_a_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every outcome, the startup failure's log line included, in the captured records. Run in `tmp_path`
    with relative paths, which the log lines keep whole (`test_the_cli_exits_78_…`)."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.chdir(tmp_path)
    valid = write(Path("valid.json"), VALUES)
    contents: list[bytes | Mapping[str, object]] = [
        document(extra="x"),
        document(operator="Quirinus\nZappelmeier"),
        document(contact="Postfach am Kuckucksweg, Zappelstadt" * 6),
        json.dumps(VALUES).encode()[:-1],
    ]
    invalid = [write(Path(f"invalid-{n}.json"), content) for n, content in enumerate(contents)]
    operator.load(valid)
    operator.at_start(valid, prod=False)
    for path in invalid:
        with pytest.raises(OperatorFileInvalid):
            operator.load(path)
        assert operator.at_start(path, prod=False) is None
    secrets = write_secrets(tmp_path / "s", **API_SECRETS)
    for path in (invalid[1], Path("absent.json")):  # an invalid file, then none: the api does not start
        env = {**PROD_API_ENV, "JOBHUNTER_SECRETS_DIR": str(secrets), "JOBHUNTER_OPERATOR_FILE": str(path)}
        with pytest.MonkeyPatch.context() as patch:
            for name in set(os.environ) - set(app_free_environ()):
                patch.delenv(name)
            for name, value in env.items():
                patch.setenv(name, value)
            with pytest.raises(SystemExit):
                startup_or_exit("api")
    records = caplog.records
    warned = [event for event in events(records) if event.get("event") == "operator_file_invalid"]
    assert [(event["path"], event["level"]) for event in warned] == [
        (str(path), "warning") for path in invalid
    ]
    assert [event["problem"] for event in warned] == [
        f"has 1 key {OTHER_KEYS}",
        "operator must contain no control character",
        "contact must be at most 200 characters",
        f"is not valid JSON (line 1, column {len(json.dumps(VALUES))})",  # where the closing brace is missing
    ]
    failed = [event for event in events(records) if event.get("event") == "startup_check_failed"]
    assert [(event["check"], event["detail"]) for event in failed] == [
        ("operator_file", "the operator file invalid-1.json operator must contain no control character"),
        ("operator_file", "the operator file absent.json is missing"),
    ]
    for record in records:
        assert_no_value(str(record.msg) + repr(record.args) + str(record.exc_info))


# --- the compose mount --------------------------------------------------------------------------------------


def values_of(details: OperatorDetails) -> list[str]:
    return [getattr(details, name) for name in FIELDS]


def binds(service: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [mount for mount in service.get("volumes", []) if mount["type"] == "bind"]


def operator_mounts(config: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    found: dict[str, list[Mapping[str, Any]]] = {}
    for name, service in config["services"].items():
        mounts = [
            mount
            for mount in binds(service)
            if mount["target"] == TARGET or Path(mount["source"]).name == "operator.json"
        ]
        if mounts:
            found[name] = mounts
    return found


def test_the_prod_compose_mounts_the_host_file_read_only_into_the_api_only() -> None:
    [mount] = operator_mounts(prod_config())["api"]
    assert set(operator_mounts(prod_config())) == {"api"}
    assert (mount["source"], mount["target"], mount.get("read_only")) == (HOST_FILE, TARGET, True)
    # a missing host file stops the api's start: no directory is made in its place
    assert mount["bind"]["create_host_path"] is False
    # the api reads it at the setting's default: no service names another path
    for service in prod_config()["services"].values():
        assert "JOBHUNTER_OPERATOR_FILE" not in service.get("environment", {})


def test_the_test_stack_mounts_its_fictional_file_in_place_of_the_host_s() -> None:
    mounts = operator_mounts(pt_config())
    assert set(mounts) == {"api"}
    [mount] = mounts["api"]
    assert (mount["source"], mount["target"], mount.get("read_only")) == (
        str(TEST_OPERATOR_FILE),
        TARGET,
        True,
    )
    assert mount["bind"]["create_host_path"] is False
    details = operator.load(TEST_OPERATOR_FILE)
    assert details is not None
    assert details.operator == "Test Operator" and details.contact == "operator@example.org"
    assert all(value.startswith("Test ") or value.endswith("@example.org") for value in values_of(details))
