# tests/unit/test_redact.py
import io
import json
import logging
import time

import structlog
from jobhunter.config import ALL_SECRETS
from jobhunter.ids import uuid7
from jobhunter.observability import redact
from jobhunter.observability.logging import configure_logging, get_logger
from jobhunter.observability.redact import mask_text, redact_event


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


def test_none_bool_and_numbers_are_left_alone_and_other_objects_become_masked_strings() -> None:
    class Contact:
        def __str__(self) -> str:
            return "write to max@example.org"

    marker = object()
    ev = redact_event(
        None,
        "info",
        {"event": "x", "n": 7, "f": 1.5, "none": None, "flag": True, "o": marker, "c": Contact()},
    )
    assert ev["n"] == 7 and ev["f"] == 1.5 and ev["none"] is None and ev["flag"] is True
    assert ev["o"] == mask_text(str(marker)) and ev["c"] == "write to [EMAIL]"


def test_tuples_and_sets_are_walked_and_emitted_as_lists() -> None:
    ev = redact_event(
        None,
        "info",
        {
            "event": "x",
            "t": ("max@example.org", 3, ("0170 1234567",)),
            "s": {"max@example.org"},
            "fs": frozenset({"max@example.org"}),
        },
    )
    assert ev["t"] == ["[EMAIL]", 3, ["[PHONE]"]]
    assert ev["s"] == ["[EMAIL]"] and ev["fs"] == ["[EMAIL]"]


def test_positional_args_and_tuple_values_never_render_the_email() -> None:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    get_logger("t").info("to %s", "max@example.org", emails=("max@example.org",))
    line = json.loads(buf.getvalue())
    assert line["event"] == "to [EMAIL]" and line["emails"] == ["[EMAIL]"]
    assert "positional_args" not in line and "example.org" not in buf.getvalue()


def test_loggers_bound_before_configure_logging_still_redact() -> None:
    early = get_logger("early").bind(component="x")
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    early.info("contact", detail="max@example.org")
    line = json.loads(buf.getvalue())
    assert line["logger"] == "early" and line["component"] == "x" and line["detail"] == "[EMAIL]"


def test_nested_dict_keys_are_masked_after_the_sensitive_key_check() -> None:
    ev = redact_event(
        None,
        "info",
        {
            "event": "x",
            "recipients": {"max@example.org": "sent", "+49 170 1234567": 1},
            "deep": [{"Token": "t", "hr@acme.de": {"password": "p"}}],
        },
    )
    assert ev["recipients"] == {"[EMAIL]": "sent", "[PHONE]": 1}
    assert ev["deep"] == [{"Token": "[REDACTED]", "[EMAIL]": {"password": "[REDACTED]"}}]


def test_ts_and_level_are_exempt_only_at_the_top_level() -> None:
    ev = redact_event(
        None,
        "info",
        {
            "event": "x",
            "ts": "2026-10-16T06:00:00.123456Z",
            "level": "info",
            "job": {"level": "senior, contact hr@acme.de", "ts": "call 0170 1234567"},
        },
    )
    assert ev["ts"] == "2026-10-16T06:00:00.123456Z" and ev["level"] == "info"
    assert ev["job"] == {"level": "senior, contact [EMAIL]", "ts": "call [PHONE]"}


def test_stdlib_library_logs_go_through_the_same_chain() -> None:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    logging.getLogger("lib").warning("contact max@example.org")
    line = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert line["logger"] == "lib" and line["level"] == "warning"
    assert {"ts", "event"} <= line.keys()
    assert "max@example.org" not in buf.getvalue()


def test_stdlib_args_exceptions_and_stack_info_are_masked() -> None:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    lib = logging.getLogger("lib")
    lib.warning("contact %s", "max@example.org")
    try:
        raise ValueError("bad address max@example.org")
    except ValueError:
        lib.error("failed", exc_info=True)
    lib.warning("stack for max@example.org", stack_info=True)

    args_line, exc_line, stack_line = (json.loads(line) for line in buf.getvalue().splitlines())
    assert args_line["event"] == "contact [EMAIL]"
    assert "ValueError: bad address [EMAIL]" in exc_line["exception"]
    assert (
        stack_line["event"] == "stack for [EMAIL]"
        and "Stack (most recent call last)" in stack_line["stack_info"]
    )
    assert "max@example.org" not in buf.getvalue()


def test_a_malformed_stdlib_call_writes_one_content_free_error_line(capsys) -> None:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    root = logging.getLogger()
    # pytest's capture handler re-raises formatting errors; keep only the handler under test.
    root.handlers[:] = [
        h for h in root.handlers if isinstance(h.formatter, structlog.stdlib.ProcessorFormatter)
    ]
    logging.getLogger("lib").info("bad %d", "max@example.org")
    assert [json.loads(line) for line in buf.getvalue().splitlines()] == [
        {"level": "error", "event": "logging_error", "logger": "lib"}
    ]
    captured = capsys.readouterr()
    assert captured.err == "" and "max@example.org" not in captured.out


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


def test_mask_text_is_bounded_on_adversarial_input() -> None:
    for text in ("a" * 200_000, "a." * 100_000):
        started = time.perf_counter()
        out = mask_text(text)
        assert time.perf_counter() - started < 1.0
        assert out.endswith("…[TRUNCATED]") and len(out) < 20_000


def test_strings_up_to_the_cap_are_not_truncated() -> None:
    at_cap = "ab " * 5461 + "a"  # 16384 characters, no secret-shaped run
    assert len(at_cap) == 16_384 and mask_text(at_cap) == at_cap
    assert mask_text(at_cap + "b").endswith("…[TRUNCATED]")


def test_emails_with_long_or_uuid_local_parts_are_masked() -> None:
    out = mask_text("from reply+5f3e2a1b-0000-7000-8000-c4d3e2f1a0b9@jobs.example.de today")
    assert out == "from [EMAIL] today"


def test_long_event_names_survive_but_random_keys_do_not() -> None:
    name = "ranking_state_recompute_skipped_for_stale_version"
    assert len(name) >= 40 and mask_text(f"event {name}") == f"event {name}"
    random_key = "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6kL9n"
    assert len(random_key) == 40 and mask_text(f"key {random_key}") == "key [KEY]"
    assert mask_text("snake_case_but_with_Capitals_and_digits_0123456789") == "[KEY]"


def test_every_secret_name_and_the_secret_suffixes_are_redacted() -> None:
    event = {"event": "x", **dict.fromkeys(sorted(ALL_SECRETS), "v")}
    event |= {
        "Serp_API_KEY": "v",
        "adzuna_app_key": "v",
        "db_password": "v",
        "ntfy_token": "v",
        "x_secret": "v",
    }
    ev = redact_event(None, "info", event)
    assert {k for k, v in ev.items() if v == "[REDACTED]"} == event.keys() - {"event"}


def test_usage_fields_that_merely_mention_tokens_or_keys_are_kept() -> None:
    fields = {"input_tokens": 12, "max_tokens": 4096, "dedup_key": "abc", "output_tokens": 3}
    assert redact_event(None, "info", {"event": "x", **fields}) == {"event": "x", **fields}


def test_secrets_in_query_strings_are_replaced() -> None:
    url = "https://api.adzuna.com/v1/api/jobs/de/search/1"
    query = "app_id=abc123&app_key=0123456789abcdef0123456789abcdef&what=python"
    message = f"Client error '401' for url '{url}?{query}'"
    assert mask_text(message) == (
        f"Client error '401' for url '{url}?app_id=[REDACTED]&app_key=[REDACTED]&what=python'"
    )
    assert mask_text("GET /search?q=x&API_KEY=s3cret#frag") == "GET /search?q=x&API_KEY=[REDACTED]#frag"
    assert mask_text("GET /x?monkey=1&dedup_key=2") == "GET /x?monkey=1&dedup_key=2"


ADZUNA_KEY = "0123456789abcdef0123456789abcdef"
PING_URL = "https://hc-ping.com/5f3e2a1b-0000-7000-8000-c4d3e2f1a0b9"


def test_registered_secret_values_are_masked_wherever_they_appear() -> None:
    assert mask_text(f"key {ADZUNA_KEY}") == f"key {ADZUNA_KEY}"  # no pattern knows this shape
    redact.register_secret_values([ADZUNA_KEY, PING_URL])
    assert mask_text(f"key {ADZUNA_KEY} and {PING_URL}/fail") == "key [SECRET] and [SECRET]/fail"
    ev = redact_event(None, "info", {"event": "x", "nested": [{"k": f"v={ADZUNA_KEY}"}]})
    assert ev["nested"] == [{"k": "v=[SECRET]"}]


def test_registering_ignores_short_and_blank_values_and_strips_the_rest() -> None:
    redact.register_secret_values(["", "   ", "short", "  padded-secret-value \n"])
    assert mask_text("short and padded-secret-value") == "short and [SECRET]"


def test_the_longest_registered_value_wins_and_registrations_accumulate() -> None:
    redact.register_secret_values(["secretvalue-one"])
    redact.register_secret_values(["secretvalue-one-and-more"])
    assert mask_text("secretvalue-one-and-more / secretvalue-one") == "[SECRET] / [SECRET]"


def test_registered_values_apply_to_rendered_log_lines() -> None:
    redact.register_secret_values([ADZUNA_KEY])
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    get_logger("t").info("calling", url=f"https://x/y/{ADZUNA_KEY}")
    logging.getLogger("lib").warning("oops %s", ADZUNA_KEY)
    assert ADZUNA_KEY not in buf.getvalue() and buf.getvalue().count("[SECRET]") == 2
