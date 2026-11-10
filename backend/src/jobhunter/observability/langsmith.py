"""LangSmith tracing with hidden inputs and outputs, one anonymizer and the published tracing state (spec
§17.8; plan M5 Task 6, O13-O16, O21, Q15-Q17, Rulings T6-1, T6-2, T6-3).

Tracing is on in the prod compose's worker only (`LANGSMITH_TRACING=true`); dev, test, CI, the E2E stack and
the spike recorders keep it unset. Only the worker installs a client: the api makes no LLM call.

**The client** (`make_client`) is `langsmith.Client(api_url=LANGSMITH_EU_ENDPOINT, api_key=<the langsmith_key
secret>, hide_inputs=True, hide_outputs=True, anonymizer=anonymize)`, and `None` while tracing is off. By
then the startup check `langsmith_config` has made sure that the hide flags are on, the endpoint is the EU
one, the key's secret is present and no write replica, API key variable or sampling rate is set (O15, Q15).
What langsmith 0.14.11 does with these, as read in its `client.py` (`_hide_run_*`, L2796-2849;
`_run_transform` and `update_run` apply them to every run it sends, created or updated):
- inputs and outputs: `hide_inputs=True` and `hide_outputs=True` return `{}` before the anonymizer is looked
  at, so no input or output reaches it. A callable `hide_*` would be skipped once an anonymizer is set (O14),
  hence the booleans, and one anonymizer for the rest;
- metadata (`extra.metadata`, with the LangChain variables and the sampling rate the client adds to it itself)
  goes through the anonymizer as a JSON round trip of itself (all of it is hidden instead when
  `LANGSMITH_HIDE_METADATA=true`);
- the error string, `repr(exc)` followed by the formatted traceback, goes through the anonymizer wrapped as
  `{"error": <string>}`, and the SDK keeps the original string when the answer holds no `error` key: so
  `anonymize` answers every such payload with one, whatever its value was.
Events keep only what is not a streamed token, and a chain run's `serialized` is dropped. A trace then carries
the graph's structure, the timings, token counts and error classes (§17.8 L5740), besides the SDK's runtime
description (its version, the platform's and Python's).

**The anonymizer** (`anonymize`): a payload that is exactly `{"error": s}` becomes `{"error": error_tag(s)}`;
any other payload keeps the keys of `METADATA_ALLOWLIST` only, LangGraph's `langgraph_node` becoming `node`
(unless the payload names a `node` itself), and `usage_metadata` (where langchain-core puts an LLM run's token
usage, O13) reduced to its integer token counts. `error_tag(s)` is `"<ErrorClass>:<code>"` (§17.8 L5739,
Ruling T6-1): the class is the last dotted part of the identifier that starts the string, when `(` follows it
at once (`Unknown` otherwise); the code is the first `code=`, `error_code=` or `error_class=` value in the
string's first line (the `repr`), its quotes stripped, when it is an identifier of at most 64 characters
(`unknown` otherwise). A value runs to the next `,` or `)` unless it is quoted, so nothing of a value that is
no identifier survives: `error_class='Bearer x'` gives `unknown`, and `Authorization: Bearer x` names neither.

**The tracing state** (`install_tracing`, `publish_tracing_state`): the worker installs the client
process-wide (`run_trees.configure`, with tracing enabled) at step 7 of its start, before it compiles any
graph, and publishes the state the install returned, `{"tracing": bool, "region": "eu" | null}`, on
`worker_heartbeat.tracing` at step 8, in a transaction that starts with its epoch fence (Q17, O16).
`/api/system/health` shows the last published state as `langsmith`: `null` until a worker published one, and
kept when its heartbeat goes stale.
"""

import re
from dataclasses import dataclass
from typing import Any, Final, Literal

from langsmith import Client, run_trees
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.config import LANGSMITH_EU_ENDPOINT, AppConfig, tracing_enabled
from jobhunter.db.sql import q

# §17.8 L5738, and the token usage (O13).
METADATA_ALLOWLIST: Final = frozenset(
    {
        "run_id",
        "cycle_id",
        "target_id",
        "attempt_no",
        "node",
        "model",
        "effort",
        "prompt_schema_hash",
        "profile_version_id",
        "usage_metadata",
    }
)
UNKNOWN_CLASS: Final = "Unknown"
UNKNOWN_CODE: Final = "unknown"
# Ruling T6-1. ASCII only: `\w` matches no other letter.
_LEADING_CLASS: Final = re.compile(r"([A-Za-z_][\w.]*)\(", re.ASCII)
_IDENTIFIER: Final = re.compile(r"[A-Za-z_]\w*", re.ASCII)
_CODE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")
# One of the three keywords (not the tail of a longer name), then its value: quoted and followed by `,`, `)`
# or the end; or else everything up to the next `,` or `)`.
_CODE_VALUE: Final = re.compile(
    r"(?<![\w.])(?:error_code|error_class|code)="
    r"(\"[^\"]*\"(?=\s*(?:[,)]|$))|'[^']*'(?=\s*(?:[,)]|$))|[^,)]*)",
    re.ASCII,
)
_DETAILS_SUFFIX: Final = "_token_details"
_PUBLISH: Final = q("UPDATE worker_heartbeat SET tracing = :tracing WHERE id = 1", jsonb=["tracing"])


@dataclass(frozen=True)
class TracingState:
    """Whether this worker traces, and where to: the EU endpoint, the only one `langsmith_config` admits."""

    tracing: bool
    region: Literal["eu"] | None


TRACING_OFF: Final = TracingState(tracing=False, region=None)


def _unquoted(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def error_tag(error: str) -> str:
    """`"<ErrorClass>:<code>"` of a run's error string; nothing else of it survives (module docstring)."""
    leading = _LEADING_CLASS.match(error)
    name = leading.group(1).rpartition(".")[2] if leading else ""
    error_class = name if _IDENTIFIER.fullmatch(name) else UNKNOWN_CLASS
    found = _CODE_VALUE.search(error.partition("\n")[0])
    value = _unquoted(found.group(1)) if found else ""
    code = value if _CODE.fullmatch(value) else UNKNOWN_CODE
    return f"{error_class}:{code}"


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _token_counts(usage: object) -> dict[str, Any]:
    """The integer counts of a `usage_metadata`, and those of its `*_token_details`; nothing else (O13)."""
    if not isinstance(usage, dict):
        return {}
    counts: dict[str, Any] = {}
    for key, value in usage.items():
        if _is_count(value):
            counts[key] = value
        elif isinstance(key, str) and key.endswith(_DETAILS_SUFFIX) and isinstance(value, dict):
            if details := {name: count for name, count in value.items() if _is_count(count)}:
                counts[key] = details
    return counts


def anonymize(payload: dict[str, Any]) -> dict[str, Any]:
    """The client's one anonymizer: an error payload's tag, or a metadata payload's allowlist (module
    docstring). An error payload always keeps its `error` key: without it the SDK would send the original."""
    if payload.keys() == {"error"}:
        error = payload["error"]
        return {"error": error_tag(error) if isinstance(error, str) else f"{UNKNOWN_CLASS}:{UNKNOWN_CODE}"}
    kept = {key: value for key, value in payload.items() if key in METADATA_ALLOWLIST}
    if "node" not in kept and "langgraph_node" in payload:
        kept["node"] = payload["langgraph_node"]
    if "usage_metadata" in kept:
        kept["usage_metadata"] = _token_counts(kept["usage_metadata"])
    return kept


def make_client(cfg: AppConfig) -> Client | None:
    """The worker's LangSmith client, `None` unless tracing is on (module docstring)."""
    if not tracing_enabled(cfg):
        return None
    key = cfg.secrets.get("langsmith_key", b"").decode("utf-8", errors="replace").strip()
    if not key:  # `langsmith_config` refuses to start without it
        raise ValueError("tracing needs the langsmith_key secret")
    return Client(
        api_url=LANGSMITH_EU_ENDPOINT,
        api_key=key,
        hide_inputs=True,
        hide_outputs=True,
        anonymizer=anonymize,
    )


def install_tracing(cfg: AppConfig) -> TracingState:
    """When tracing is on, configure the SDK's process-wide client (langchain-core's tracer takes the same
    one) with tracing enabled; the state to publish either way. Called before any graph is compiled."""
    client = make_client(cfg)
    if client is None:
        return TRACING_OFF
    # `enabled`: the SDK traces exactly when this configuration says so, whatever the variable's case.
    run_trees.configure(client=client, enabled=True)
    return TracingState(tracing=True, region="eu")


async def publish_tracing_state(conn: AsyncConnection, state: TracingState) -> None:
    """Write `state` on `worker_heartbeat.tracing`, in the caller's transaction, which starts with the
    worker's epoch fence (`fence_epoch`), so the row is this worker's."""
    await conn.execute(_PUBLISH, {"tracing": {"tracing": state.tracing, "region": state.region}})
