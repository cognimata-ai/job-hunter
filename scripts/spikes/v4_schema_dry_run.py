"""Spike V4 (spec §22 V4, §10.11; plan Task 7): the schema dry-run's cases, sent through the budgeted
recorder.

    uv run python scripts/spikes/record_anthropic.py --spike v4 --dry-run|--live|--commit-recorded
    uv run python scripts/spikes/v4_schema_dry_run.py --record-probe-count

`calls()` gives the gating cases `llm.schema_dry_run.cases` makes of prompt-schema version 1 (built offline:
`V1`; M2's version) and the seeded model policy (`settings_v1.json`): the generator schema on Sonnet, then the
critic, profile and queries schemas on Opus. Each is the request the gateway sends for that case
(`schema_dry_run.prompt_for` and `node_for`): the dry-run's static system block and synthetic user turn,
effort `low`, `max_tokens` 2,000, purpose `schema_dry_run`, and the version's stored wire schema, as the
gateway passes it.

`RECORD_EVERY_ANSWER`: the recorder commits each case's live answer, a stream or an error response, as the
fake's answer and goes on to the next case (controller rulings R7-2, R7-3), so the dry-run on the fake replays
the live results. The generator case was answered 400 before this flag existed; `--commit-recorded` writes its
answer from the raw capture, sending nothing.

The informational `minimum` probe is never a paid message (R7-2): `--record-probe-count` sends the dry-run's
probe body (`schema_dry_run.probe_count_body`) to the free `messages.count_tokens` endpoint, through
`record_anthropic.counting_gateway`, and commits the answer (its status and its JSON body without the
`request_id`) where the fake serves it (`fake.count_recording_path`); the raw answer goes to
`.fixtures-raw/anthropic/v4/`.

`check(observation)` is the verdict of one call (field paths, flags and counts, never content): for a stream
the stop reason and the served model, the sent schema byte-equal to the transformed one, a parsed output, and
the paths of any value the output holds that the schema does not allow (a stripped `enum`, `anyOf` or `$ref`);
for an error response its status, type and message (API text). Its `missing` lists the checks that failed.
"""

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Final

import anthropic
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import fake, schema_dry_run
from jobhunter.llm.prompts import render
from jobhunter.llm.schemas import SCHEMAS_V1, transformed
from jobhunter.types import get_object

RECORD_EVERY_ANSWER: Final = True
HERE: Final = Path(__file__).resolve().parent
REPO: Final = HERE.parents[1]
KEY_VAR: Final = "JOBHUNTER_ANTHROPIC_API_KEY"


def _policy() -> dict[str, Any]:
    return get_object(load_settings_v1(), "scoring.model_policy")


def _v1() -> render.PromptVersion:
    """Prompt-schema version 1 as the M2 seed stored it (the id is never sent)."""
    templates, schemas = render.prompt_schema_v1()
    return render.PromptVersion(
        id=1,
        prompt_schema_hash=render.prompt_schema_hash(templates, schemas),
        templates={name: str(text) for name, text in templates.items()},
        schemas=schemas,
        parser_family="generator_v1",
    )


V1: Final = _v1()


def _file_name(label: str) -> str:
    return label.replace("/", "-").replace(":", "-")


def calls() -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
    for case in schema_dry_run.cases(_policy()):
        if case.probe:  # answered by the free counting endpoint, never as a message (R7-2)
            continue
        prompt = schema_dry_run.prompt_for(case)
        node = schema_dry_run.node_for(case, V1)
        (message,) = prompt.messages
        planned.append(
            {
                "case": _file_name(case.label),
                "purpose": node.purpose,
                "model": node.model,
                "effort": node.effort,
                "max_tokens": node.max_tokens,
                "system": [block.text for block in prompt.system],
                "user": message.content,
                "schema": dict(node.schema.json),  # the stored wire schema, as the gateway passes it
            }
        )
    return planned


def _canonical(schema: Any) -> str:
    return fake.canonical_schema_json(schema)


def _schema_name(sent: Any) -> str | None:
    """Which of `SCHEMAS_V1` a request sent."""
    return next((n for n, m in SCHEMAS_V1.items() if _canonical(sent) == _canonical(transformed(m))), None)


def check(observation: dict[str, Any]) -> dict[str, Any]:
    body = observation["request"]
    sent = body.get("output_config", {}).get("format", {}).get("schema")
    name = _schema_name(sent)
    if "events" not in observation:  # an error response
        return {
            "missing": ["http_200"],
            "case": name,
            "schema_bytes_equal": name is not None,
            "http_status": observation["status"],
            "error_type": observation["error_type"],
            "error_message": observation["error_message"],
        }
    meta = observation["response_metadata"]
    events = [fake.SSEEvent(e["name"], e["data"]) for e in observation["events"]]
    try:
        output = json.loads(fake.text_of(events))
    except ValueError:
        output = None
    violations = schema_dry_run.schema_violations(transformed(SCHEMAS_V1[name]), output) if name else ["$"]
    checks = {
        "end_turn": meta.get("stop_reason") == "end_turn",
        "model_served": meta.get("model_name") == observation["requested_model"],
        "schema_bytes_equal": name is not None,
        "parsed": bool(observation["parsed_ok"]) and output is not None,
        "enums_allowed": not violations,
    }
    usage = observation["usage_metadata"]
    return {
        "missing": [check for check, ok in checks.items() if not ok],
        "case": name,
        "stop_reason": meta.get("stop_reason"),
        "checks": checks,
        "violations": violations,
        "usage": {key: usage.get(key) for key in ("input_tokens", "output_tokens")},
    }


# ---- the probe on the counting endpoint (free) -------------------------------------------------------------


def _recorder() -> Any:
    spec = importlib.util.spec_from_file_location("jobhunter_spike_recorder", HERE / "record_anthropic.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("the recorder is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def record_probe_count(key: str, *, fixtures: Path, raw: Path) -> str:
    """Send the probe's count body to `messages.count_tokens` and commit its answer for the fake; the line
    printed names the status and the API's message only."""
    recorder = _recorder()
    case = next(c for c in schema_dry_run.cases(_policy()) if c.probe)
    async with recorder.counting_gateway(key) as gateway:
        body = schema_dry_run.probe_count_body(gateway, case, V1)
        try:
            counted = await gateway.count_tokens(case.model, body)
            status, answer = 200, {"input_tokens": counted}
        except anthropic.APIStatusError as exc:
            status, answer = exc.status_code, exc.body if isinstance(exc.body, dict) else {}
    answer = {k: v for k, v in answer.items() if k != "request_id"}
    recorded = {"status": status, "body": answer}
    text = json.dumps(recorded, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if key and key in text:
        raise RuntimeError("the count answer holds the key; nothing written")
    path = fake.count_recording_path(fixtures, case.model, body["output_config"]["format"]["schema"])
    for target in (path, raw / f"{_file_name(case.label)}.count.json"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
    message = answer.get("error", {}).get("message") if isinstance(answer.get("error"), dict) else None
    return f"{case.label}: count_tokens {status} {message or answer} -> {path.relative_to(REPO)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="v4_schema_dry_run.py", description=(__doc__ or "").split("\n\n")[0]
    )
    parser.add_argument("--record-probe-count", action="store_true", required=True)
    parser.parse_args(sys.argv[1:] if argv is None else argv)
    key = os.environ.get(KEY_VAR, "").strip()
    if not key:
        print(f"v4: REFUSED, {KEY_VAR} is not set; nothing sent")
        return 4
    try:
        line = asyncio.run(
            record_probe_count(
                key,
                fixtures=REPO / "tests/contract/fixtures/anthropic",
                raw=REPO / ".fixtures-raw/anthropic/v4",
            )
        )
    except Exception as exc:  # noqa: BLE001 - the class only: an SDK error holds its request
        print(f"v4: {type(exc).__name__}")
        return 1
    print(line)
    return 0


if __name__ == "__main__":
    # As the recorder: no ANTHROPIC_* variable may steer the request (G1).
    for name in [name for name in os.environ if name.startswith("ANTHROPIC_")]:
        del os.environ[name]
    raise SystemExit(main())
