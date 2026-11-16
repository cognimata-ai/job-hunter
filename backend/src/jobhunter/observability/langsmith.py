"""LangSmith tracing with hidden inputs and outputs, one anonymizer and the published tracing state (spec
§17.8; plan M5 Task 6, O13-O16, O21, Q15-Q17, Rulings T6-1, T6-2, T6-3, T6-8).

Tracing is on in the prod compose's worker only (`LANGSMITH_TRACING=true`); dev, test, CI, the E2E stack and
the spike recorders keep it unset. Only the worker installs a client. Every other process is explicitly
untraced, whatever its environment says (`disable_tracing`, which `cli.main` runs first for every command but
`worker`): the api, and a worker-role command that runs in the worker's environment and may call the LLM in
its own process, such as `schema-dry-run`. Without that, the SDK would trace such a process through its
default client, built from the environment, which hides inputs and outputs but has no anonymizer.

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
A trace then carries the graph's structure, the timings, token counts and error classes (§17.8 L5740). Also
sent, outside the anonymizer, and none of it content:
- the run names (a graph's or node's name, a runnable's class or function name) and the tags (LangGraph's
  `graph:step:<n>` and `seq:step:<n>`, and the tracer's own, none of them set by the app);
- an LLM run's `extra.invocation_params`: the model's parameters and the request's bound options, the wire
  JSON schema of `output_config` among them, which is the schema of a node's output, not the output;
- an LLM run's `serialized` manifest: the chat model's constructor arguments (model, `max_tokens`, effort,
  timeout, base URL, streaming), its key only as the reference `{"type": "secret", "id":
  ["ANTHROPIC_API_KEY"]}` (langchain-core's `dumpd` never writes a secret's value). A chain run's `serialized`
  is dropped by the SDK;
- `extra.runtime`, the SDK's runtime description: its version, the platform's and Python's.
**The events invariant**: the SDK strips `kwargs` from `new_token` events only (the streamed text), and passes
any other event as it is. langchain-core's `retry` event (`Runnable.with_retry`, a callback's `on_retry`) puts
`str(exception)` or `str(result)` in its `kwargs`, which can be content: so no traced code may use
`with_retry` or `on_retry`. Nothing in the app does (the gateway retries outside langchain-core; LangGraph's
own retry emits no event).

**The anonymizer** (`anonymize`): a payload that is exactly `{"error": s}` becomes `{"error": error_tag(s)}`;
any other payload keeps the keys of `METADATA_ALLOWLIST` only, LangGraph's `langgraph_node` becoming `node`
(unless the payload names a `node` itself), and of those only a value that is a string of at most
`MAX_VALUE_CHARS` (128) characters, an integer or `None` (no list, no mapping, nothing longer); and
`usage_metadata` (where langchain-core puts an LLM run's token usage, O13) reduced to its integer token
counts. `error_tag(s)` is `"<ErrorClass>:<code>"` (§17.8 L5739, Ruling T6-1): the class is the last dotted
part of the identifier that starts the string, when `(` follows it at once (`Unknown` otherwise); the code is
the first `code=`, `error_code=` or `error_class=` value in the string's first line (the `repr`), its quotes
stripped, when it is an identifier of at most 64 characters (`unknown` otherwise). A value runs to the next
`,` or `)` unless it is quoted, and is not trimmed, so nothing of a value that is no identifier survives:
`error_class='Bearer x'` and `code= abc` give `unknown`, and `Authorization: Bearer x` names neither.
When no keyword is found, a `NodeFailed` gives the code of its fixed message (Ruling T6-5): its repr is
`NodeFailed('node <node> failed: <ErrorClass>')`, where `graphs.sanitize` puts the failed error's class name
(no content), and when the repr is exactly that, both names identifiers of at most 64 characters, the code is
`<ErrorClass>`. langchain-core appends the formatted traceback to the repr without a line break, so the
traceback's fixed `Traceback (most recent call last):` header is taken off the first line's end before the
repr is matched; any other text, quoting or class gives `unknown`, as before.

**At the worker's exit** (M5 Task 21, Ruling T21-3 I3): an unreachable LangSmith never holds it. In prod
the SDK's threads are daemons (`LANGSMITH_USE_DAEMON=true`; the SDK reads it once, at its first client, so it
is set in the compose environment, not here) and runs are sent uncompressed
(`LANGSMITH_DISABLE_RUN_COMPRESSION=true`: the compressed path sends through a module-level thread pool that
the interpreter joins at exit whatever the daemon setting says); `langsmith_config` refuses a prod worker that
traces without both. Once its shutdown is complete, the worker gives the client's queue at most
`FLUSH_TIMEOUT_S` to drain (`flush_tracing`, off the event loop); what is still queued then is dropped with
the daemon threads. Neither call cancels a request in flight: the daemon threads are what bound the exit.

**The tracing state** (`install_tracing`, `publish_tracing_state`): the worker installs the client
process-wide (`run_trees.configure`, with tracing enabled) at step 7 of its start, before it compiles any
graph, and publishes the state the install returned, `{"tracing": bool, "region": "eu" | null}`, on
`worker_heartbeat.tracing` at step 8, in a transaction that starts with its epoch fence (Q17, O16).
`/api/system/health` shows the last published state as `langsmith`: `null` until a worker published one, and
kept when its heartbeat goes stale.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from langsmith import Client, run_trees

from jobhunter.config import LANGSMITH_EU_ENDPOINT, AppConfig, tracing_enabled

if TYPE_CHECKING:  # the database layer is imported by the publish only: `cli.main` imports this module for
    # every command, the container healthchecks included, which load no database stack
    from sqlalchemy.ext.asyncio import AsyncConnection

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
# The longest string an allowlisted metadata key keeps (Fix round 1, M1).
MAX_VALUE_CHARS: Final = 128
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
# Ruling T6-5: the repr of `errors.NodeFailed(node, error_class)`, whose message is `node <node> failed:
# <error_class>`; and the header of the traceback langchain-core appends to a repr (`_get_stacktrace`).
_NODE_FAILED_CLASS: Final = "NodeFailed"
_NODE_FAILED_REPR: Final = re.compile(
    r"NodeFailed\('node [A-Za-z_][A-Za-z0-9_]{0,63} failed: ([A-Za-z_][A-Za-z0-9_]{0,63})'\)"
)
_TRACEBACK_HEADER: Final = "Traceback (most recent call last):"
_DETAILS_SUFFIX: Final = "_token_details"
_PUBLISH_SQL: Final = "UPDATE worker_heartbeat SET tracing = :tracing WHERE id = 1"
# The longest the worker's exit waits for the queued runs to be sent (module docstring, "At the worker's
# exit").
FLUSH_TIMEOUT_S: Final = 5.0
# The client `install_tracing` installed in this process, which `flush_tracing` flushes (none while off).
_INSTALLED: list[Client] = []


@dataclass(frozen=True)
class TracingState:
    """Whether this worker traces, and where to: the EU endpoint, the only one `langsmith_config` admits."""

    tracing: bool
    region: Literal["eu"] | None


TRACING_OFF: Final = TracingState(tracing=False, region=None)


def _unquoted(value: str) -> str:
    """`value` without one pair of enclosing quotes; nothing else is taken off (T6-1: no trimming)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def error_tag(error: str) -> str:
    """`"<ErrorClass>:<code>"` of a run's error string; nothing else of it survives (module docstring).

    The code comes from the first line, the repr: the value of its first `code=`, `error_code=` or
    `error_class=` keyword; or, with no such keyword, for a `NodeFailed`, the error class its fixed message
    names (Ruling T6-5); else it is `unknown`.
    """
    leading = _LEADING_CLASS.match(error)
    name = leading.group(1).rpartition(".")[2] if leading else ""
    error_class = name if _IDENTIFIER.fullmatch(name) else UNKNOWN_CLASS
    first_line = error.partition("\n")[0]
    found = _CODE_VALUE.search(first_line)
    if found:
        value = _unquoted(found.group(1))
    elif error_class == _NODE_FAILED_CLASS and (
        node_failed := _NODE_FAILED_REPR.fullmatch(first_line.removesuffix(_TRACEBACK_HEADER))
    ):
        value = node_failed.group(1)
    else:
        value = ""
    code = value if _CODE.fullmatch(value) else UNKNOWN_CODE
    return f"{error_class}:{code}"


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _kept_value(value: object) -> bool:
    """What an allowlisted key other than `usage_metadata` may hold: a short string, an integer or `None`."""
    return value is None or _is_count(value) or (isinstance(value, str) and len(value) <= MAX_VALUE_CHARS)


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
    kept = {
        key: value
        for key, value in payload.items()
        if key in METADATA_ALLOWLIST and key != "usage_metadata" and _kept_value(value)
    }
    if "node" not in kept and "langgraph_node" in payload and _kept_value(payload["langgraph_node"]):
        kept["node"] = payload["langgraph_node"]
    if "usage_metadata" in payload:
        kept["usage_metadata"] = _token_counts(payload["usage_metadata"])
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
    _INSTALLED[:] = [client]
    return TracingState(tracing=True, region="eu")


def flush_tracing(timeout_s: float = FLUSH_TIMEOUT_S) -> None:
    """Give the installed client's queue at most `timeout_s` to be sent; nothing when tracing is off. It
    blocks, so the worker calls it off its event loop, after `worker_shutdown_complete` (module docstring)."""
    for client in _INSTALLED:
        client.flush(timeout=timeout_s)


def disable_tracing() -> None:
    """Tracing off for this process, whatever its environment says: every process but the worker (§17.8 "only
    the worker traces", Ruling T6-8; `cli.main` calls it before any command but `worker` runs).

    `run_trees.configure(enabled=False)` sets the SDK's switch, the context's and the process-wide one, which
    `langsmith.utils.tracing_is_enabled` reads before the environment, and so does langchain-core's choice of
    a tracer (`tracers.context._tracing_v2_is_enabled`): no tracer and no client is made. The process has
    cached no client when it runs (none is made before a traced call), so none needs dropping: `client` is
    left alone.
    """
    run_trees.configure(enabled=False)


async def publish_tracing_state(conn: "AsyncConnection", state: TracingState) -> None:
    """Write `state` on `worker_heartbeat.tracing`, in the caller's transaction, which starts with the
    worker's epoch fence (`fence_epoch`), so the row is this worker's."""
    from jobhunter.db.sql import q  # here: see the import of `AsyncConnection`

    statement = q(_PUBLISH_SQL, jsonb=["tracing"])
    await conn.execute(statement, {"tracing": {"tracing": state.tracing, "region": state.region}})
