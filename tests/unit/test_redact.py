# tests/unit/test_redact.py
import io
import json
import logging
import time

import pytest
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
    for text in ("a" * 200_000, "a." * 100_000, "a@" * 100_000, "+1 " * 100_000):
        started = time.perf_counter()
        out = mask_text(text)
        assert time.perf_counter() - started < 1.0
        assert len(out) < 20_000
    assert mask_text("a." * 100_000).count("…[TRUNCATED]…") == 1
    assert mask_text("a" * 200_000) == "[KEY]"  # masking left nothing long enough to cut


def test_megabyte_inputs_are_masked_in_bounded_time() -> None:
    redact.register_secret_values([f"registered-secret-value-{i:02d}-abcdef" for i in range(20)])
    for text in ("word, " * 500_000, "a." * 1_500_000, "0123 456 " * 333_334, "x@y.de " * 430_000):
        assert len(text) > 2_900_000
        started = time.perf_counter()
        out = mask_text(text)
        assert time.perf_counter() - started < 2.0
        assert len(out) <= 16_384 + len("…[TRUNCATED]…")


def test_strings_up_to_the_cap_are_not_truncated() -> None:
    at_cap = "ab " * 5461 + "a"  # 16384 characters, no secret-shaped run
    assert len(at_cap) == 16_384 and mask_text(at_cap) == at_cap
    assert "…[TRUNCATED]…" in mask_text(at_cap + "b")


def test_long_strings_keep_their_head_and_their_tail() -> None:
    head = "Traceback (most recent call last):\n"
    tail = "ValueError: the final line, reply to max@example.org"
    out = mask_text(head + "  File filler, in frame\n" * 5000 + tail)
    assert out.startswith(head) and out.endswith("ValueError: the final line, reply to [EMAIL]")
    assert out.count("…[TRUNCATED]…") == 1 and len(out) <= 16_384 + len("…[TRUNCATED]…")
    assert out.index("…[TRUNCATED]…") == 8192


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


def test_encoded_forms_of_registered_values_are_masked_too() -> None:
    value = "Zm9vYmFy+/Zm9vYmFy=="
    redact.register_secret_values([value])
    dsn = "postgresql://jobhunter_app:Zm9vYmFy%2B%2FZm9vYmFy%3D%3D@db.internal:5432/jobhunter"
    assert mask_text(dsn) == "postgresql://jobhunter_app:[SECRET]@db.internal:5432/jobhunter"
    assert mask_text(f"raw {value}") == "raw [SECRET]"


def test_plus_and_percent_encoded_spaces_are_both_registered() -> None:
    redact.register_secret_values(["pass word value"])
    assert mask_text("a=pass+word+value&b=pass%20word%20value&c=pass word value") == (
        "a=[SECRET]&b=[SECRET]&c=[SECRET]"
    )


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("detail with max@example.org")


def test_self_referencing_containers_log_without_raising_and_render_recursion() -> None:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    items: list[object] = ["a"]
    items.append(items)
    mapping: dict[str, object] = {"k": 1}
    mapping["me"] = mapping
    get_logger("t").info("cycles", items=items, mapping=mapping)
    line = json.loads(buf.getvalue())
    assert line["items"] == ["a", "[RECURSION]"]
    assert line["mapping"] == {"k": 1, "me": "[RECURSION]"}


def test_containers_nested_deeper_than_32_levels_are_cut_off() -> None:
    deep: object = "bottom"
    for _ in range(40):
        deep = [deep]
    node = redact_event(None, "info", {"event": "x", "deep": deep})["deep"]
    levels = 0
    while isinstance(node, list):
        node, levels = node[0], levels + 1
    assert levels == 32 and node == "[RECURSION]"


def test_a_container_that_appears_twice_without_a_cycle_is_rendered_both_times() -> None:
    shared = ["max@example.org"]
    ev = redact_event(None, "info", {"event": "x", "a": shared, "b": [shared, shared]})
    assert ev["a"] == ["[EMAIL]"] and ev["b"] == [["[EMAIL]"], ["[EMAIL]"]]


def test_a_redaction_failure_yields_a_fail_closed_event_without_other_fields() -> None:
    ev = redact_event(
        None,
        "info",
        {"event": "x", "ts": "T", "level": "info", "logger": "t", "bad": Unprintable(), "other": "kept?"},
    )
    assert ev == {
        "ts": "T",
        "level": "info",
        "logger": "t",
        "event": "redaction_failed",
        "error_type": "RuntimeError",
    }
    assert redact_event(None, "info", {"event": "x", "bad": Unprintable()}) == {
        "event": "redaction_failed",
        "error_type": "RuntimeError",
    }


def test_a_redaction_failure_in_a_log_call_renders_only_the_fail_closed_event() -> None:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    get_logger("t").info("hello", bad=Unprintable(), other="max@example.org")
    line = json.loads(buf.getvalue())
    assert line.keys() == {"ts", "level", "logger", "event", "error_type"}
    assert line["event"] == "redaction_failed" and line["error_type"] == "RuntimeError"
    assert line["logger"] == "t" and "detail" not in buf.getvalue() and "example.org" not in buf.getvalue()


SECRET32 = "Q7xT2vLmB9wKd4RnE6yUa1ZcH8sJf3Pg"
FILLER = "ab " * 4000


def test_a_registered_secret_across_the_head_cut_leaves_no_fragment() -> None:
    assert len(SECRET32) == 32
    redact.register_secret_values([SECRET32])
    out = mask_text(FILLER[: 8192 - 16] + SECRET32 + FILLER)
    assert "…[TRUNCATED]…" in out
    assert not any(SECRET32[i : i + 6] in out for i in range(0, 27, 3))


def test_a_registered_secret_across_the_tail_cut_leaves_no_fragment() -> None:
    redact.register_secret_values([SECRET32])
    out = mask_text(FILLER + SECRET32 + FILLER[: 8192 - 16])
    assert "…[TRUNCATED]…" in out
    assert not any(SECRET32[i : i + 6] in out for i in range(0, 27, 3))


def test_an_email_across_the_head_or_tail_cut_leaves_no_fragment() -> None:
    address = "max.mustermann@example.org"
    for text in (
        FILLER[: 8192 - 14] + address + FILLER,
        FILLER + address + FILLER[: 8192 - 12],
    ):
        out = mask_text(text)
        assert "…[TRUNCATED]…" in out
        assert not any(part in out for part in ("max.", "muster", "mann@", "@exam", "example"))


def test_sensitive_nested_keys_are_masked_like_any_other_key() -> None:
    ev = redact_event(
        None,
        "info",
        {
            "event": "x",
            "recipients": {
                "cv.mueller@example.org": "sent",
                "profile max@example.org": 1,
                "hr@acme.de_token": "t",
            },
        },
    )
    assert ev["recipients"] == {
        "[EMAIL]": "[REDACTED]",
        "profile [EMAIL]": "[REDACTED]",
        "[EMAIL]_token": "[REDACTED]",
    }
    assert "example.org" not in json.dumps(ev) and "acme.de" not in json.dumps(ev)


def test_non_string_nested_keys_are_emitted_as_masked_strings() -> None:
    ev = redact_event(None, "info", {"event": "x", "m": {("max@example.org", 1): "x", 5: "y"}})
    assert ev["m"] == {"('[EMAIL]', 1)": "x", "5": "y"}


def test_a_malformed_positional_call_does_not_raise_and_still_masks() -> None:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    log = get_logger("t")
    log.info("done", "max@example.org")
    log.info("x %d", "str")
    log.info("to %(who)s", {"who": "max@example.org"})
    first, second, third = (json.loads(line) for line in buf.getvalue().splitlines())
    assert first["event"] == "done" and first["positional_args"] == ["[EMAIL]"]
    assert second["event"] == "x %d" and second["positional_args"] == ["str"]
    assert third["event"] == "to [EMAIL]" and "positional_args" not in third
    assert "example.org" not in buf.getvalue()


class BrokenStream(io.StringIO):
    def write(self, s: str) -> int:
        raise RuntimeError("disk gone")


def test_handle_error_never_raises_even_when_the_stream_is_broken() -> None:
    configure_logging("INFO", stream=BrokenStream())
    logging.getLogger("lib").info("hello")  # must not raise out of the log call


def test_before_configure_logging_stdlib_records_reach_last_resort_masked(capsys, monkeypatch) -> None:
    lonely = logging.getLogger("lonely")  # no handler anywhere on its path
    monkeypatch.setattr(lonely, "propagate", False)
    lonely.warning("contact max@example.org")
    err = capsys.readouterr().err
    line = json.loads(err)
    assert line["logger"] == "lonely" and line["level"] == "warning"
    assert line["event"] == "contact [EMAIL]" and "example.org" not in err


def test_the_path_segment_and_the_other_scheme_of_a_registered_url_are_masked() -> None:
    uuid_text = "5f3e2a1b-0000-7000-8000-c4d3e2f1a0b9"
    redact.register_secret_values([f"https://hc-ping.com/{uuid_text}"])
    assert mask_text(f"ping {uuid_text} done") == "ping [SECRET] done"
    assert mask_text(f"http://hc-ping.com/{uuid_text}/fail") == "[SECRET]/fail"
    assert mask_text(f"https://hc-ping.com/{uuid_text}") == "[SECRET]"
    redact.register_secret_values(["https://example.org/v1"])
    assert mask_text("see v1 and https://example.org/v1") == "see v1 and [SECRET]"  # "v1" is too short


def test_secrets_in_json_bodies_and_authorization_schemes_are_replaced() -> None:
    body = '{"access_token": "abc123", "Client_Secret":"s3 cr3t", "name": "keep", "refresh_token" : "r"}'
    expected = '{"access_token": "[REDACTED]", "Client_Secret":"[REDACTED]", "name": "keep", '
    assert mask_text(body) == expected + '"refresh_token" : "[REDACTED]"}'
    assert mask_text("failed with Bearer eyJhbGciOi.abc-def_ghi= for x") == (
        "failed with Bearer [REDACTED] for x"
    )
    assert mask_text("header Authorization: Basic dXNlcjpwYXNz") == "header Authorization: Basic [REDACTED]"
    assert mask_text("GET /x?refresh_token=abc&client_secret=def") == (
        "GET /x?refresh_token=[REDACTED]&client_secret=[REDACTED]"
    )


@pytest.mark.parametrize(
    ("args", "kwargs", "expected"),
    [
        (
            ("took %d ms for %s", float("inf"), "max@example.org"),
            {},
            {"event": "took %d ms for %s", "positional_args": ["Infinity", "[EMAIL]"]},
        ),
        (
            ("char %c for %s", 0x110000, "max@example.org"),
            {},
            {"event": "char %c for %s", "positional_args": [0x110000, "[EMAIL]"]},
        ),
        (
            ("to %s: %s", "max@example.org", Unprintable()),
            {},
            {"event": "redaction_failed", "error_type": "RuntimeError"},
        ),
        (
            ("x",),
            {"positional_args": 5, "to": "max@example.org"},
            {"event": "x", "positional_args": 5, "to": "[EMAIL]"},
        ),
    ],
    ids=["infinite-float-for-d", "out-of-range-c", "argument-str-raises", "positional-args-keyword"],
)
def test_positional_formatting_never_raises_into_the_caller(args, kwargs, expected) -> None:
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    get_logger("t").info(*args, **kwargs)
    line = json.loads(buf.getvalue())
    assert expected.items() <= line.items()
    assert "example.org" not in buf.getvalue() and "detail" not in buf.getvalue()


HALF = 524_288  # the first and the last HALF characters of an input over 1 MiB are masked
EMAIL_NEEDLE = "max.mustermann@example.org"


def _base64_run(length: int) -> str:
    return ("QUJD" * (length // 4 + 1))[:length]


def _fragments(needle: str, out: str) -> list[str]:
    return [needle[i : i + 6] for i in range(0, len(needle) - 5, 3) if needle[i : i + 6] in out]


@pytest.mark.parametrize("needle", [SECRET32, EMAIL_NEEDLE], ids=["secret", "email"])
@pytest.mark.parametrize("offset", [0, -512, -700, 100], ids=["join", "join-512", "join-700", "join+100"])
def test_a_secret_or_email_near_the_megabyte_head_join_leaves_no_fragment(needle, offset) -> None:
    redact.register_secret_values([SECRET32])
    # The needle straddles character HALF + offset. The base64-like run before it masks to `[KEY]`, which
    # brings the inner end of the head into the first 8192 characters of the output.
    start = HALF + offset - len(needle) // 2
    text = _base64_run(start - 1) + " " + needle + " " + "ab " * 400_000
    assert len(text) > 1_700_000 and text.index(needle) == start
    out = mask_text(text)
    assert "…[TRUNCATED]…" in out and _fragments(needle, out) == []


@pytest.mark.parametrize("needle", [SECRET32, EMAIL_NEEDLE], ids=["secret", "email"])
@pytest.mark.parametrize("offset", [0, -512, 512], ids=["join", "join-512", "join+512"])
def test_a_secret_or_email_near_the_megabyte_tail_join_leaves_no_fragment(needle, offset) -> None:
    redact.register_secret_values([SECRET32])
    # The needle straddles character len(text) - (HALF + offset). The base64-like run after it masks to
    # `[KEY]`, which brings the inner end of the tail into the last 8192 characters of the output.
    after = HALF + offset - (len(needle) - len(needle) // 2)
    body = needle + " " + _base64_run(after - 1)
    text = "ab " * ((1_720_000 - len(body)) // 3) + " " + body
    assert text.index(needle) == len(text) - (HALF + offset) - len(needle) // 2
    out = mask_text(text)
    assert "…[TRUNCATED]…" in out and _fragments(needle, out) == []


def test_the_join_margin_grows_with_the_longest_registered_secret() -> None:
    passphrase = " ".join(f"word{i:03d}" for i in range(120))  # 959 characters, no key-shaped run
    redact.register_secret_values([passphrase])
    start = HALF - 900  # 900 of its characters lie before the join, more than the minimum margin of 512
    text = _base64_run(start - 1) + " " + passphrase + " " + "ab " * 400_000
    assert _fragments(passphrase, mask_text(text)) == []


def test_quoted_secrets_in_python_reprs_and_escaped_json_are_replaced() -> None:
    assert mask_text("{'access_token': 'abc'}") == "{'access_token': '[REDACTED]'}"
    # An escaped quote does not end the value, so the rest of an escaped JSON string is redacted too.
    assert mask_text('{\\"token\\": \\"abc\\"}') == '{\\"token\\": \\"[REDACTED]'
    assert mask_text('"password": "ab\\"cdefgh"') == '"password": "[REDACTED]"'
    # A value ends only at the quote that opened it.
    assert mask_text('{"password": "don\'t-tell-anyone"}') == '{"password": "[REDACTED]"}'
    assert mask_text("{'password': \"it's-a-secret\"}") == "{'password': \"[REDACTED]\"}"


def test_quoted_secret_and_authorization_patterns_stay_linear_on_megabyte_inputs() -> None:
    for text in (
        '{"token": "' + "\\a" * 1_500_000,
        "'key':'" * 430_000,
        '\\"key\\": \\"' * 280_000,
        '"key"' + " " * 3_000_000,
        "authorization: " * 200_000,
        "bearer " + "a" * 3_000_000,
        "Bearer " * 430_000,
    ):
        assert len(text) > 2_900_000
        started = time.perf_counter()
        out = mask_text(text)
        assert time.perf_counter() - started < 2.0
        assert len(out) <= 16_384 + len("…[TRUNCATED]…")


def test_every_long_path_segment_of_a_registered_url_is_masked() -> None:
    ping_key = "fH3kQ9zR2mX7vB1nL5wT8c"
    redact.register_secret_values([f"https://hc-ping.com/{ping_key}/nightly-backup"])
    assert mask_text(f"pinging {ping_key}/start") == "pinging [SECRET]/start"
    assert mask_text(f"http://hc-ping.com/{ping_key}/other-job") == "http://hc-ping.com/[SECRET]/other-job"
    assert mask_text("slug nightly-backup") == "slug [SECRET]"


def _reject_constant(name: str) -> object:
    raise ValueError(f"not strict JSON: {name}")


def test_non_finite_floats_render_as_strings_so_every_line_is_strict_json() -> None:
    nan, inf = float("nan"), float("inf")
    ev = redact_event(None, "info", {"event": "x", "v": [nan, inf, -inf], "n": {"r": nan}, "ok": 1.5})
    assert ev["v"] == ["NaN", "Infinity", "-Infinity"] and ev["n"] == {"r": "NaN"} and ev["ok"] == 1.5
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    get_logger("t").info("floats", values=[nan, inf, -inf], ratio=nan, nested={"x": -inf}, took=1.5)
    line = json.loads(buf.getvalue(), parse_constant=_reject_constant)
    assert line["values"] == ["NaN", "Infinity", "-Infinity"] and line["ratio"] == "NaN"
    assert line["nested"] == {"x": "-Infinity"} and line["took"] == 1.5


def test_bearer_and_basic_in_prose_stay_but_credentials_are_masked() -> None:
    assert mask_text("basic setup done") == "basic setup done"
    assert mask_text("Bearer of bad news") == "Bearer of bad news"
    assert mask_text("sent Bearer ya29.a0AfH6SMBx3kQ to x") == "sent Bearer [REDACTED] to x"
    assert mask_text("with Basic am9iaHVudGVyOnMzY3JldA==") == "with Basic [REDACTED]"
    # After an `Authorization` header name, any credential is masked, however it looks.
    assert mask_text("authorization=bearer abcdefgh") == "authorization=bearer [REDACTED]"
    assert mask_text("Authorization : Basic dXNlcjpwYXNz") == "Authorization : Basic [REDACTED]"


def test_quoted_authorization_header_values_are_redacted_whole() -> None:
    assert mask_text("{'Authorization': 'Basic dXNlcjpwYXNz'}") == "{'Authorization': '[REDACTED]'}"
    assert mask_text('{"authorization": "Basic dXNlcjpwYXNz"}') == '{"authorization": "[REDACTED]"}'
    assert mask_text('{\\"Authorization\\": \\"Bearer abcdefgh\\"}') == '{\\"Authorization\\": \\"[REDACTED]'
