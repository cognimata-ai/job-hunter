# tests/unit/test_redact.py
import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog
from jobhunter.ids import uuid7
from jobhunter.observability.logging import configure_logging, get_logger
from jobhunter.observability.redact import mask_text, redact_event


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """`configure_logging` changes process-wide state; give every test the state it found."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    noisy = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, noisy_level in noisy.items():
        logging.getLogger(name).setLevel(noisy_level)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def test_listed_keys_and_prefixes_are_redacted() -> None:
    ev = redact_event(
        None,
        "info",
        {
            "event": "x",
            "Authorization": "Bearer abc",
            "password": "p",
            "description_text": "ad",
            "profile_json": {},
            "cv_file": "f",
            "raw_text_enc": "r",
            "nested": {"set-cookie": "c", "ok": 1},
            "run_id": 7,
        },
    )
    assert ev["Authorization"] == ev["password"] == ev["description_text"] == "[REDACTED]"
    assert ev["profile_json"] == ev["cv_file"] == ev["raw_text_enc"] == "[REDACTED]"
    assert ev["nested"] == {"set-cookie": "[REDACTED]", "ok": 1} and ev["run_id"] == 7


def test_values_are_masked() -> None:
    out = mask_text(
        "mail max.mustermann@example.org, call +49 170 1234567 or 0170 1234567, "
        "key sk-ant-api03-AbCdEf0123456789xyzXYZ"
    )
    assert "example.org" not in out and "1234567" not in out and "AbCdEf" not in out
    assert out.count("[EMAIL]") == 1 and out.count("[PHONE]") == 2 and out.count("[KEY]") == 1


def test_dates_and_uuids_survive() -> None:
    fixed = "01929345-0123-7123-8123-012345678901"  # digit-heavy uuid7 shape
    for rid in (str(uuid7()), fixed):
        assert mask_text(f"2026-10-16T06:00:00+00:00 {rid}") == f"2026-10-16T06:00:00+00:00 {rid}"


def test_json_output_has_spec_fields_and_redaction_runs_last() -> None:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    structlog.contextvars.bind_contextvars(request_id="r1", token="t0ps3cret")
    get_logger("t").info("run_started", run_id=3, detail="write to max@example.org")
    structlog.contextvars.clear_contextvars()
    line = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert {"ts", "level", "event", "logger", "request_id", "run_id"} <= line.keys()
    assert line["token"] == "[REDACTED]" and "max@example.org" not in buf.getvalue()


def test_redaction_matches_keys_case_insensitively_and_replaces_whole_values() -> None:
    ev = redact_event(
        None,
        "info",
        {
            "event": "x",
            "TOKEN": ["a", {"b": 1}],
            "Cv_File": None,
            "RAW_TEXT": 3,
            "Description": {"deep": "x"},
            "X-RapidAPI-Key": "k",
        },
    )
    assert ev["TOKEN"] == ev["Cv_File"] == ev["RAW_TEXT"] == "[REDACTED]"
    assert ev["Description"] == ev["X-RapidAPI-Key"] == "[REDACTED]"


def test_redaction_recurses_into_lists_without_mutating_the_caller_s_containers() -> None:
    inner = {"password": "p", "who": "max@example.org"}
    items = ["call 0170 1234567", inner, ["x@example.org"]]
    original = {"event": "x", "items": items, "ts": "2026-10-16T06:00:00Z", "level": "info"}
    ev = redact_event(None, "info", original)
    assert ev["items"] == ["call [PHONE]", {"password": "[REDACTED]", "who": "[EMAIL]"}, ["[EMAIL]"]]
    assert items == ["call 0170 1234567", inner, ["x@example.org"]]
    assert inner == {"password": "p", "who": "max@example.org"}
    assert ev["ts"] == "2026-10-16T06:00:00Z" and ev["level"] == "info"


def test_values_that_are_not_strings_dicts_or_lists_are_left_alone() -> None:
    marker = object()
    ev = redact_event(None, "info", {"event": "x", "n": 7, "f": 1.5, "none": None, "flag": True, "o": marker})
    assert ev["n"] == 7 and ev["f"] == 1.5 and ev["none"] is None and ev["flag"] is True
    assert ev["o"] is marker


def test_stdlib_library_logs_go_through_the_same_chain() -> None:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    logging.getLogger("lib").warning("contact max@example.org")
    line = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert line["logger"] == "lib" and line["level"] == "warning"
    assert {"ts", "event"} <= line.keys()
    assert "max@example.org" not in buf.getvalue()


def test_http_client_loggers_stay_quiet_because_they_log_full_urls() -> None:
    buf = io.StringIO()
    configure_logging("DEBUG", stream=buf)
    logging.getLogger("httpx").info("HTTP Request: GET https://api.example/search?api_key=hunter2")
    logging.getLogger("httpcore.http11").debug("send_request_headers")
    assert buf.getvalue() == ""


def test_configure_logging_is_idempotent_and_leaves_foreign_handlers_alone() -> None:
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    first, second = io.StringIO(), io.StringIO()
    configure_logging("INFO", stream=first)
    configure_logging("INFO", stream=second)
    get_logger("t").info("once")
    assert first.getvalue() == "" and len(second.getvalue().strip().splitlines()) == 1
    ours = [h for h in root.handlers if isinstance(h.formatter, structlog.stdlib.ProcessorFormatter)]
    assert len(ours) == 1
    assert foreign in root.handlers


def test_the_level_argument_filters_events() -> None:
    buf = io.StringIO()
    configure_logging("WARNING", stream=buf)
    get_logger("t").info("quiet")
    get_logger("t").warning("loud")
    assert [json.loads(line)["event"] for line in buf.getvalue().splitlines()] == ["loud"]
