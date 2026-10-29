"""The budgeted recorder of the paid Anthropic spikes (plan Task 4; plan Global Constraints, "Paid API").

    uv run python scripts/spikes/record_anthropic.py --spike {v13,v4,v19,v2probe} --dry-run|--live
    uv run python scripts/spikes/record_anthropic.py --spike {v4v2,v5,v14} --dry-run|--live --through-gateway
    uv run python scripts/spikes/record_anthropic.py --spike v12 --dry-run|--live

It is the only way a spike sends a paid message, and the only place that maps `JOBHUNTER_ANTHROPIC_API_KEY`
to a secret: the key is written to a temporary `anthropic_key` file, read into a worker `AppConfig` as every
worker reads its secrets, and the file is deleted at once. The key is never printed, logged or written
anywhere else: the captured request drops `x-api-key`, every written byte has the key's value removed, and an
exception is printed as its class only.

The calls go through the production code: `gateway._chat` on `transport.make_http_client(cfg, mount=…)`,
where the mount is a capturing transport over the network (a direct connection; it asks for an identity
encoding, so the captured stream is the bytes the SDK parsed).

**Budget.** One budget covers every Anthropic spike, M2's included: `MAX_ACTUAL_USD` ($10.00) of actual
spend in total, set by the user for M3. Before every send the recorder adds three amounts:
- the settled cost of every `done` entry;
- the worst case of every entry that is not `done`, because its outcome is unknown. An entry without a
  settled cost also counts at its worst case.
- the worst case of the call about to be sent: the exact `pricing.worst_case_usd` of its request, its
  `extra_usd` included.

The recorder refuses the send when that sum is above `MAX_ACTUAL_USD` (`Budget.refusal`), and also when the
spike has sent all of its own calls (`SPIKE_CALLS`). There is no message count and no cap on the sum of worst
cases: those were M2's limits (8 messages, $0.80).

The whole invocation holds an exclusive `flock` on `.fixtures-raw/anthropic-budget.lock`, so a second
invocation fails at once. Before every send, `.fixtures-raw/anthropic-budget.json` (`{"entries": [...],
"sent", "worst_case_usd", "settled_usd", "limits"}`) is rewritten by atomic rename with the message counted
(`"state": "sending"`) and its worst case recorded. Only then is a transport created, so a crash after that
line leaves the message counted at its worst case. After the response the entry is `done` with its status
and its settled cost:
- from the recorded usage when the stream completed;
- $0 for an error response (no content);
- its worst case when the outcome is unknown.

A case that has an entry is never sent again, and the first unexpected outcome stops the spike.

**Files.**
- `.fixtures-raw/anthropic/<spike>/<case>.request.json` (the sent request without its key),
  `<case>.response.headers.json`, `<case>.response.sse` (or `.response.json` for an error) and
  `<case>.check.json` (the spike's field-path verdict): raw, gitignored, never committed.
- `tests/contract/fixtures/anthropic/<purpose>/<model>/<schema12>.sse`: the committed recording of a
  synthetic-input call, in the layout `llm/fake.py` serves. It keeps no provider identity and no thinking: the
  message id is replaced, thinking text and signatures become `fake.THINKING_OMITTED` and
  `fake.SIGNATURE_OMITTED`, and of the response headers only `content-type` is kept (so `request-id`,
  `anthropic-organization-id` and `cf-ray` are removed) in `<schema12>.headers.json`.
  `<schema12>.components.json` holds the byte size of each rendered component of the sent request (system
  blocks, messages, the `output_config` schema as canonical JSON) and the recorded usage, so a payload test
  can compute the recording's input bound without the request capture; a native-PDF call's also holds its
  `native_pdf_count_tokens` and its `input_tokens_bound`.
- A spike whose module sets `RECORD_EVERY_ANSWER = True` (V4: the dry-run replays each case's live result,
  controller ruling R7-3) commits whatever answer a call got and goes on to its next call: a completed stream
  whatever its stop reason, or an error response, whose body is committed without its `request_id` at the same
  path (its status and `content-type` in `<schema12>.headers.json`, and no sidecar: an error has no usage). It
  still counts every send first and never sends a case twice. `--commit-recorded` writes that committed error
  answer from the raw capture of a counted case (one answered before its spike recorded every answer); it
  sends nothing and needs no key.
- A spike whose module sets `RAW_ONLY = True` (v2probe) commits nothing. Each answer goes to the spike's
  check: a completed stream whatever its stop reason, or an error response. The check's `expected` flag
  decides whether the spike goes on, and the raw files stay in `.fixtures-raw`. Any other outcome, such as
  a failed transport or a cut stream, stops the spike.

**Spikes.** A spike is a module next to this one (`SPIKE_MODULES`) that sends nothing itself: `calls()` gives
its calls in order, as dicts with `case`, `purpose`, `model`, `effort`, `max_tokens`, `system` (a list of
texts), `user` (the one user message: a text, or a list of content blocks with an explicit
`input_tokens_bound` or `native_pdf_count_tokens`), `schema` (a JSON schema dict, or a Pydantic model as the
gateway passes its node's: langchain needs a `title` on a dict schema, which is then sent too) and optionally
`extra_usd` (a decimal string), `input_tokens_bound` and `native_pdf_count_tokens` (the call's `count_tokens`:
its bound is `bounds.native_pdf_bound` of it); `check(observation)` returns the spike's verdict of one call
(field paths, never content). A module may also define `wanted(case, verdicts)`, its own ladder. Before each
send the recorder asks it whether the case is still needed, given the `verdict` of each counted case whose
check has run (read from `<case>.check.json`). A case that is not wanted is neither counted nor sent. V13 is
`v13_structured_stream.py`, V4 `v4_schema_dry_run.py`, V19 `v19_token_bound.py` and v2probe (V4b, the
generator schema candidates of prompt-schema version 2) `v4b_generator_v2_probe.py`.
`counting_gateway(key)` gives the spikes a gateway for the free `messages.count_tokens` endpoint, with the
key mapped as a live run maps it. The M3 spikes v4v2 (`v4_schema_dry_run.py`), V12 (`v12_batches.py`), V5 and
V14 (`v5_stream_timing.py`) run in the gateway and batch modes below.

`--dry-run` sends nothing, needs no key and writes nothing: it prints the plan, each call's input bound and
worst case, the budget the live run would meet, and the request each call would send, built by the same code
on a transport that answers locally. For a spike with a ladder, its budget line assumes that every case is
sent.

**The gateway spikes** (M3 Task 26: `GATEWAY_SPIKES`, v4v2, v5 and v14; `--through-gateway` states the mode,
which these spikes always run in, and is refused for any other). The spike's calls go through the real
gateway, `LLMGateway.call()`, on a throwaway database (`tests/support`'s PostgreSQL harness, migrated and
seeded, removed with its data at the end), whose `httpx2` client sends through
`BudgetedTransport(TimingTransport(network))`:
- `BudgetedTransport` authorises **every HTTP attempt** before it leaves: it reserves the attempt's worst
  case (the gateway's own text bound of the request it carries, `message_figures`) in the budget file as an
  entry `<case>#<attempt>` and counts it against the spike's `SPIKE_CALLS` and `MAX_ACTUAL_USD` by the rule
  above, so a transport retry of the gateway consumes the allowance instead of exceeding it. An attempt the
  rule refuses, one for a call an earlier run counted, one that matches no planned call, one after the spike
  stopped (a definitive 4xx answer stops it), and any route but the paid ones and the free ones (a
  `count_tokens` POST, a Message Batches GET) are answered with a synthetic 400 `invalid_request_error` made
  here: nothing reaches the network, and the gateway takes it as a definitive `LLMRequestError` and does not
  retry. Each answered attempt is settled as a message above (its usage, $0 for an error response or a
  connect failure before the send, its worst case when the stream was cut).
- `TimingTransport` keeps the exact request body of every exchange and stamps every response chunk with the
  monotonic clock, so each SSE event has the time it arrived (`Exchange.event_times`).
The spike's module plans its logical calls (`gateway_calls(spike)`: `case`, `node`, `prompt`) and runs them
(`run_gateway(spike, session)` with a `GatewaySession`), one at a time; it commits the streams it keeps
(`session.commit_stream`, the layout above; a name suffix `<schema12>.<case>.sse` beside the fake's key) and
returns its report, written raw as `report.json`. Every exchange is written raw, its timing included.
`--dry-run` builds each planned call's request through `gateway._chat` on `to_messages` (what one try of
`LLMGateway.call` sends) on a transport that answers locally: no database, no key, nothing written.

**The batch spike** (`BATCH_SPIKES`, v12). The module plans the items (`batch_requests(spike)`); their params
are `LLMGateway.batch_request_body`. The create goes through `BatchesClient.create` on the same budgeted
transport, which reserves the batch's worst case, the sum of its items' batch worst cases, as one entry that
stays `created` (counted at its worst case) until the results are read. The recorder polls `retrieve` every
`BATCH_POLL_S` up to `BATCH_MAX_WAIT` (`--max-wait-hours` shortens it) or until the batch ends, writing each
exchange raw as it completes, streams the results, settles the entry at the
sum of the items' batch costs (an errored, canceled or expired item $0, a missing one its worst case), and
lists the workspace until it finds the batch (an empty listing is never proof of absence). A run after a crash
resumes polling a `created` entry's batch and never creates a second one. The create, every retrieve, the
results and the listing page are written raw, and committed under `batches/<spike>/` without provider ids
(`create.json`, `retrieve.json`, `results.json`, `list.json` with the spike's batch only).

**LangSmith.** langchain traces a call to LangSmith (its prompt and output included) when tracing is on in
the environment, which the SDK's own telemetry switch does not cover. So with any of `TRACING_VARS` set, every
mode refuses at once and calls nothing (final review FR-13); the refusal names the variables, never a value.

**Modes.** The raw files, the budget and its lock are 0600 in 0700 directories; the committed recordings are
0644 in 0755 directories, whatever the umask, so the dev stack's worker (uid 10001) can serve a recording
another uid made (final review FR-14). The key file is 0600 in a 0700 temporary directory.
"""

import argparse
import asyncio
import codecs
import contextlib
import copy
import fcntl
import importlib.util
import itertools
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, Final, TextIO, cast

import anthropic
import httpx2
from jobhunter.config import AppConfig, load_config
from jobhunter.llm import bounds, fake, pricing
from jobhunter.llm.batches import BatchApiError, BatchesClient, BatchRequest, Created, ItemResult
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import (
    RETRYABLE_STATUSES,
    LLMGateway,
    NodeConfig,
    RenderedPrompt,
    _chat,
    to_messages,
)
from jobhunter.llm.transport import ANTHROPIC_BASE_URL, make_http_client
from jobhunter.observability.logging import configure_logging
from jobhunter.observability.redact import register_secret_values
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

HERE: Final = Path(__file__).resolve().parent
REPO: Final = HERE.parents[1]
# The repository's directories, whatever the cwd: one budget for the whole milestone (there is no --root
# flag; `main(root=..., fixtures_out=...)` is for tests only).
DEFAULT_ROOT: Final = REPO / ".fixtures-raw"
DEFAULT_FIXTURES: Final = REPO / "tests/contract/fixtures/anthropic"
KEY_VAR: Final = "JOBHUNTER_ANTHROPIC_API_KEY"
RAW_FILE_MODE: Final = 0o600
RAW_DIR_MODE: Final = 0o700
FIXTURE_FILE_MODE: Final = 0o644
FIXTURE_DIR_MODE: Final = 0o755
# Any of these makes langchain (or langsmith under it) trace a call to LangSmith, prompts and outputs
# included, or gives it the key to (final review FR-13): the recorder refuses to start.
TRACING_VARS: Final = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
)

# Every Anthropic spike, M2's included: settled + the worst case of what is not settled + the next call's.
MAX_ACTUAL_USD: Final = Decimal("10.00")
BUDGET_RULE: Final = (
    "refuse a send when settled_usd of the done entries + worst_case_usd of every other entry + the next "
    "call's worst_case_usd > actual_usd"
)
# V4: the 4 gating cases, no probe message (R7-2). v2probe: the ladder A, B, C, at most 3 sends. M3 Task 26:
# the gateway spikes count HTTP attempts (v4v2's 4 cases, V14's pair; V5's 20 calls plus up to 10 enlarged
# max_tokens stages, Ruling T26-c), V12 its one batch.
SPIKE_CALLS: Final = {"v13": 2, "v4": 4, "v19": 1, "v2probe": 3, "v4v2": 4, "v12": 1, "v5": 30, "v14": 2}
SPIKE_MODULES: Final = {
    "v13": "v13_structured_stream",
    "v4": "v4_schema_dry_run",
    "v19": "v19_token_bound",
    "v2probe": "v4b_generator_v2_probe",
    "v4v2": "v4_schema_dry_run",
    "v12": "v12_batches",
    "v5": "v5_stream_timing",
    "v14": "v5_stream_timing",
}
GATEWAY_SPIKES: Final = frozenset({"v4v2", "v5", "v14"})
BATCH_SPIKES: Final = frozenset({"v12"})
BATCH_CASE: Final = "batch"  # the logical case of a batch spike's one create
BATCH_POLL_S: Final = 60.0
BATCH_MAX_WAIT: Final = timedelta(hours=24)
MESSAGES_PATH: Final = "/v1/messages"
COUNT_TOKENS_PATH: Final = "/v1/messages/count_tokens"
DRY_RUN_KEY: Final = "dry-run-placeholder-key"
BANNED_REQUEST_KEYS: Final = ("tool_choice", "tools", "temperature", "top_p", "top_k", "betas", "fallbacks")
# The response headers a committed recording keeps; every other one (request-id, anthropic-organization-id,
# cf-ray, the rate-limit counters of the account) is removed.
COMMITTED_HEADERS: Final = ("content-type",)
_SECRET_HEADERS: Final = frozenset({"x-api-key", "authorization", "cookie", "proxy-authorization"})

OK, FAILED, LOCKED, REFUSED, STOPPED = 0, 1, 3, 4, 5

TransportFactory = Callable[[], httpx2.AsyncBaseTransport]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[object]]
DatabaseFactory = Callable[[], AbstractAsyncContextManager[AsyncEngine]]


# --- the plan ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedCall:
    spike: str
    case: str
    purpose: str
    model: pricing.Model
    effort: pricing.Effort
    max_tokens: int
    system: tuple[str, ...]
    user: str | tuple[Mapping[str, Any], ...]
    schema: Mapping[str, Any] | type[BaseModel]
    extra_usd: Decimal = Decimal(0)
    bound_override: int | None = None
    native_pdf_count_tokens: int | None = None

    @property
    def transformed_schema(self) -> dict[str, Any]:
        """The `output_config.format.schema` the call sends."""
        if isinstance(self.schema, type):
            return anthropic.transform_schema(self.schema)
        return anthropic.transform_schema(copy.deepcopy(dict(self.schema)))

    def input_tokens_bound(self) -> int:
        if self.native_pdf_count_tokens is not None:
            return bounds.native_pdf_bound(self.model, self.native_pdf_count_tokens)
        if self.bound_override is not None:
            return self.bound_override
        if not isinstance(self.user, str):
            raise ValueError(f"{self.spike} {self.case}: content blocks need an explicit input_tokens_bound")
        rendered = bounds.RenderedRequest(
            system=self.system,
            messages=(self.user,),
            schema_json=fake.canonical_schema_json(self.transformed_schema),
        )
        return bounds.text_input_bound(self.model, rendered)

    def worst_case_usd(self) -> Decimal:
        return pricing.worst_case_usd(
            self.model,
            input_tokens_bound=self.input_tokens_bound(),
            max_tokens=self.max_tokens,
            extra_usd=self.extra_usd,
        )

    def messages(self) -> list[BaseMessage]:
        content: Any = self.user if isinstance(self.user, str) else [dict(block) for block in self.user]
        return [*(SystemMessage(text) for text in self.system), HumanMessage(content)]


def load_spike(spike: str) -> ModuleType | None:
    """The definition module of `spike`, or None when there is none yet."""
    path = HERE / f"{SPIKE_MODULES[spike]}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"jobhunter_spike_{spike}", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan(spike: str, module: ModuleType) -> list[PlannedCall]:
    """The validated calls of `module.calls()`."""
    calls: list[PlannedCall] = []
    for raw in module.calls():
        model, effort = raw["model"], raw["effort"]
        if model not in pricing.MODELS or effort not in pricing.SUPPORTED_EFFORTS[model]:
            raise ValueError(f"{spike} {raw['case']}: model {model!r} with effort {effort!r} is not allowed")
        user = raw["user"] if isinstance(raw["user"], str) else tuple(raw["user"])
        if raw.get("input_tokens_bound") is not None and raw.get("native_pdf_count_tokens") is not None:
            raise ValueError(
                f"{spike} {raw['case']}: an input_tokens_bound or a native_pdf_count_tokens, not both"
            )
        calls.append(
            PlannedCall(
                spike=spike,
                case=str(raw["case"]),
                purpose=str(raw["purpose"]),
                model=model,
                effort=effort,
                max_tokens=int(raw["max_tokens"]),
                system=tuple(str(s) for s in raw["system"]),
                user=user,
                schema=raw["schema"],
                extra_usd=Decimal(str(raw.get("extra_usd", "0"))),
                bound_override=raw.get("input_tokens_bound"),
                native_pdf_count_tokens=raw.get("native_pdf_count_tokens"),
            )
        )
    if len({c.case for c in calls}) != len(calls):
        raise ValueError(f"{spike}: case names must be unique")
    return calls


# --- the budget file ---------------------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class Tree:
    """A directory the recorder writes under, with the modes of the files and directories it writes there."""

    root: Path
    file_mode: int
    dir_mode: int

    @classmethod
    def raw(cls, root: Path) -> "Tree":
        """`.fixtures-raw`: the captures, the budget and its lock, private (0600 in 0700 directories)."""
        return cls(root, RAW_FILE_MODE, RAW_DIR_MODE)

    @classmethod
    def committed(cls, root: Path) -> "Tree":
        """`tests/contract/fixtures/anthropic`: readable by every uid (0644 in 0755 directories), so the dev
        stack's worker (uid 10001) serves what another uid recorded (final review FR-14)."""
        return cls(root, FIXTURE_FILE_MODE, FIXTURE_DIR_MODE)


def _directories(tree: Tree, directory: Path) -> None:
    """`directory`, and every directory from `tree.root` down to it, at `tree.dir_mode` whatever the umask."""
    tree.root.mkdir(parents=True, exist_ok=True)
    current = tree.root
    os.chmod(current, tree.dir_mode)
    for part in directory.relative_to(tree.root).parts:
        current = current / part
        current.mkdir(exist_ok=True)
        os.chmod(current, tree.dir_mode)


def _atomic_write(tree: Tree, path: Path, data: bytes) -> None:
    """`data` at `path` under `tree`, at the tree's modes whatever the umask, by atomic rename."""
    _directories(tree, path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, tree.file_mode)
    try:
        os.fchmod(fd, tree.file_mode)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class Budget:
    """`.fixtures-raw/anthropic-budget.json`; the totals are always recomputed from the entries."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "anthropic-budget.json"
        data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        self.entries: list[dict[str, Any]] = list(data.get("entries", []))

    def sent(self) -> int:
        return len(self.entries)

    def worst_case_sum(self) -> Decimal:
        return sum((Decimal(str(e["worst_case_usd"])) for e in self.entries), Decimal(0))

    def settled_sum(self) -> Decimal:
        return sum((Decimal(str(e["settled_usd"])) for e in self.entries if e.get("settled_usd")), Decimal(0))

    @staticmethod
    def _settled(entry: Mapping[str, Any]) -> bool:
        return entry.get("state") == "done" and entry.get("settled_usd") is not None

    def settled_done_usd(self) -> Decimal:
        """The settled cost of the `done` entries."""
        return sum(
            (Decimal(str(e["settled_usd"])) for e in self.entries if self._settled(e)),
            Decimal(0),
        )

    def unsettled_worst_case_usd(self) -> Decimal:
        """The worst case of every entry that is not `done` with a settled cost (its outcome is unknown)."""
        return sum(
            (Decimal(str(e["worst_case_usd"])) for e in self.entries if not self._settled(e)),
            Decimal(0),
        )

    def committed_usd(self) -> Decimal:
        """What the budget already holds: the settled cost of the done entries plus the worst case of the
        others."""
        return self.settled_done_usd() + self.unsettled_worst_case_usd()

    def has(self, call: PlannedCall) -> bool:
        return self.entry(call) is not None

    def entry(self, call: PlannedCall) -> dict[str, Any] | None:
        """The counted entry of `call`, if it has one."""
        same = (e for e in self.entries if e.get("spike") == call.spike and e.get("case") == call.case)
        return next(same, None)

    def refusal(self, call: PlannedCall, worst_case: Decimal) -> str | None:
        """Why `call` must not be sent, or None."""
        return self.refusal_for(call.spike, worst_case)

    def refusal_for(self, spike: str, worst_case: Decimal) -> str | None:
        """Why the next send of `spike` (a message, an HTTP attempt or a batch), whose worst case is
        `worst_case`, must not leave, or None: the spike's own count, then the budget rule."""
        spike_sent = sum(1 for e in self.entries if e.get("spike") == spike)
        if spike_sent + 1 > SPIKE_CALLS[spike]:
            return f"{spike} has sent {spike_sent} of its {SPIKE_CALLS[spike]} calls"
        settled, unsettled = self.settled_done_usd(), self.unsettled_worst_case_usd()
        if settled + unsettled + worst_case > MAX_ACTUAL_USD:
            return (
                f"the budget would hold ${settled + unsettled + worst_case} (${settled} settled + "
                f"${unsettled} worst case not settled + ${worst_case} this call's worst case), "
                f"above ${MAX_ACTUAL_USD}"
            )
        return None

    def logical_entries(self, spike: str, logical: str) -> list[dict[str, Any]]:
        """The entries of the logical call `logical` of `spike`: its HTTP attempts (`logical_case`), or the
        one entry of a call counted as a message."""
        return [
            e
            for e in self.entries
            if e.get("spike") == spike and e.get("logical_case", e.get("case")) == logical
        ]

    def add(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Append `entry` and save the file: counted before its send."""
        self.entries.append(entry)
        self.save()
        return entry

    def count(self, call: PlannedCall, worst_case: Decimal, now: datetime) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "spike": call.spike,
            "case": call.case,
            "purpose": call.purpose,
            "model": call.model,
            "effort": call.effort,
            "max_tokens": call.max_tokens,
            "input_tokens_bound": call.input_tokens_bound(),
            "extra_usd": str(call.extra_usd),
            "worst_case_usd": str(worst_case),
            "state": "sending",
            "sent_at": _iso(now),
            "status": None,
            "error": None,
            "settled_usd": None,
            "settlement": None,
            "usage": None,
            "stop_reason": None,
        }
        return self.add(entry)  # counted before the send: a crash after this line leaves it counted

    def save(self) -> None:
        doc = {
            "limits": {
                "actual_usd": str(MAX_ACTUAL_USD),
                "rule": BUDGET_RULE,
                "spikes": SPIKE_CALLS,
            },
            "sent": self.sent(),
            "worst_case_usd": str(self.worst_case_sum()),
            "settled_usd": str(self.settled_sum()),
            "entries": self.entries,
        }
        _atomic_write(
            Tree.raw(self.root), self.path, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )

    def left(self) -> str:
        committed = self.committed_usd()
        return f"committed_usd={committed} left_usd={MAX_ACTUAL_USD - committed}"


@contextmanager
def exclusive(root: Path) -> Iterator[bool]:
    """Hold the exclusive `flock` on `anthropic-budget.lock` for the whole invocation; False when it is
    taken."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / "anthropic-budget.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


# --- the key -----------------------------------------------------------------------------------------------


def _worker_config(key: str) -> AppConfig:
    """A worker config whose `anthropic_key` secret is `key`, read from a temporary secret file that is gone
    when this returns."""
    directory = Path(tempfile.mkdtemp(prefix="jobhunter-spike-"))
    try:
        fd = os.open(directory / "anthropic_key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key.encode("utf-8"))
        finally:
            os.close(fd)
        return load_config("worker", {"JOBHUNTER_ENV": "dev", "JOBHUNTER_DB_NAME": "spike"}, directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@asynccontextmanager
async def counting_gateway(key: str) -> AsyncIterator[LLMGateway]:
    """A gateway for the free `messages.count_tokens` endpoint (V4's probe, V19): the key mapped as a live run
    maps it and registered with the log redaction, a direct connection to the API, and an engine that is never
    connected (a count reserves nothing). Its owner must never call `LLMGateway.call` on it."""
    register_secret_values([key])
    cfg = _worker_config(key)
    engine = create_async_engine("postgresql+psycopg://unused@127.0.0.1:1/unused")
    try:
        async with make_http_client(cfg, mount=httpx2.AsyncHTTPTransport()) as client:
            yield LLMGateway(engine, cfg, http_client=client, cache=CacheMonitor())
    finally:
        await engine.dispose()


def _without_key(data: bytes, key: str) -> bytes:
    return data.replace(key.encode("utf-8"), b"[secret:" + KEY_VAR.encode() + b"]") if key else data


# --- the capturing transport -------------------------------------------------------------------------------


class _Tee(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream, sink: bytearray) -> None:
        self._inner = inner
        self._sink = sink

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._sink.extend(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class Capture(httpx2.AsyncBaseTransport):
    """Records the one request it forwards (without its credentials) and the response bytes as they stream."""

    inner: httpx2.AsyncBaseTransport
    request: dict[str, Any] | None = None
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytearray = field(default_factory=bytearray)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        content = await request.aread()
        request.headers["accept-encoding"] = "identity"
        self.request = {
            "method": request.method,
            "url": str(request.url),
            "headers": {k: v for k, v in request.headers.items() if k.lower() not in _SECRET_HEADERS},
            "body": json.loads(content) if content else None,
        }
        response = await self.inner.handle_async_request(request)
        self.status, self.headers = response.status_code, dict(response.headers)
        stream = response.stream
        if not isinstance(stream, httpx2.AsyncByteStream):
            raise TypeError("the network transport answered with a sync stream")
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_Tee(stream, self.body),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


class _AnswersLocally(httpx2.AsyncBaseTransport):
    """The dry run's network: a 400 for every request, so nothing is sent anywhere."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        error = {"type": "error", "error": {"type": "invalid_request_error", "message": "dry run: not sent"}}
        return httpx2.Response(400, json=error)


@dataclass
class Outcome:
    capture: Capture
    out: dict[str, Any] | None
    error: str | None  # the exception's class only


async def _send(call: PlannedCall, cfg: AppConfig, inner: httpx2.AsyncBaseTransport) -> Outcome:
    """One call through the gateway's client factory, with the key of the config's `anthropic_key` secret (a
    placeholder in a dry run, whose config has none)."""
    secret = cfg.secrets.get("anthropic_key")
    key = secret.decode("utf-8") if secret else "dry-run-placeholder-key"
    capture = Capture(inner)
    async with make_http_client(cfg, mount=capture) as client:
        runnable = _chat(
            call.model,
            call.effort,
            call.max_tokens,
            call.schema if isinstance(call.schema, type) else dict(call.schema),
            api_key=SecretStr(key),
            http_client=client,
            purpose=call.purpose,
        )
        try:
            out = await runnable.ainvoke(call.messages())
        except Exception as exc:  # noqa: BLE001 - recorded by class only: its text can carry request data
            return Outcome(capture, None, type(exc).__name__)
    return Outcome(capture, out, None)


# --- every HTTP attempt authorised and timed (M3 Task 26) ---------------------------------------------------

# The case of the attempt a `BudgetedTransport` forwards, for the `TimingTransport` under it.
ATTEMPT_CASE: Final[ContextVar[str | None]] = ContextVar("jobhunter_recorder_attempt_case", default=None)
# A transport error that proves no byte of the request left (the gateway's `_BEFORE_SEND`).
_BEFORE_SEND: Final = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)


def refused_answer(why: str) -> httpx2.Response:
    """The answer of an attempt the budget refuses, made here (module docstring): a 400
    `invalid_request_error`, which the gateway takes as a definitive request error and does not retry."""
    message = f"the recorder refused the attempt: {why}; nothing sent"
    return httpx2.Response(
        400, json={"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    )


@dataclass(frozen=True)
class AttemptFigures:
    """What the budget reserves for one paid send: the model, the input bound, `max_tokens`, the worst case,
    the effort and, for a batch create, the number of items."""

    model: pricing.Model
    input_tokens_bound: int
    max_tokens: int
    worst_case_usd: Decimal
    effort: str | None
    items: int | None = None


def message_figures(body: Mapping[str, Any], *, batch: bool = False) -> AttemptFigures:
    """The gateway's reservation of one message request, or of one batch item's params with `batch`: its text
    bound (`bounds.text_input_bound` of the system texts, the message texts and the output schema as canonical
    JSON, the components `RenderedPrompt.as_request` bounds) at `pricing.worst_case_usd` with its
    `max_tokens`. A request the gateway would not bound by bytes (a non-text block, no output schema, no
    positive `max_tokens`, an unknown model) is a `ValueError`."""
    model = body.get("model")
    if not isinstance(model, str) or model not in pricing.MODELS:
        raise ValueError("a request for no known model")
    for message in body.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, str) and any(block.get("type") != "text" for block in content):
            raise ValueError("a non-text block has no byte bound")
    output_config = body.get("output_config") or {}
    schema = (output_config.get("format") or {}).get("schema")
    max_tokens = body.get("max_tokens")
    if not isinstance(schema, dict) or not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
        raise ValueError("a request without an output schema or max_tokens")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    rendered = bounds.RenderedRequest(
        system=tuple(_system_texts(body)),
        messages=tuple(_message_texts(body)),
        schema_json=fake.canonical_schema_json(schema),
    )
    known = cast(pricing.Model, model)
    bound = bounds.text_input_bound(known, rendered)
    worst = pricing.worst_case_usd(known, input_tokens_bound=bound, max_tokens=max_tokens, batch=batch)
    effort = output_config.get("effort")
    return AttemptFigures(known, bound, max_tokens, worst, effort if isinstance(effort, str) else None)


def batch_figures(body: Mapping[str, Any]) -> AttemptFigures:
    """A batch create's reservation: the sum of its items' batch worst cases (`message_figures(params,
    batch=True)`), all of one model."""
    requests = body.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ValueError("a batch create without requests")
    items = [message_figures(request["params"], batch=True) for request in requests]
    if len({item.model for item in items}) != 1:
        raise ValueError("a batch of more than one model")
    return AttemptFigures(
        model=items[0].model,
        input_tokens_bound=sum(item.input_tokens_bound for item in items),
        max_tokens=max(item.max_tokens for item in items),
        worst_case_usd=sum((item.worst_case_usd for item in items), Decimal(0)),
        effort=items[0].effort,
        items=len(items),
    )


class _SettlingStream(httpx2.AsyncByteStream):
    """A response body that calls `on_end(body, error)` once, when it ends, fails or is closed."""

    def __init__(self, inner: httpx2.AsyncByteStream, on_end: Callable[[bytes, str | None], None]) -> None:
        self._inner = inner
        self._on_end = on_end
        self._body = bytearray()
        self._ended = False

    def _end(self, error: str | None) -> None:
        if not self._ended:
            self._ended = True
            self._on_end(bytes(self._body), error)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._inner:
                self._body.extend(chunk)
                yield chunk
        except BaseException as exc:
            self._end(type(exc).__name__)
            raise
        self._end(None)

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._end("closed")


class BudgetedTransport(httpx2.AsyncBaseTransport):
    """Authorises every paid HTTP attempt of `spike` before it leaves (module docstring, "The gateway
    spikes"). `case` names the logical call the spike is making (`GatewaySession.case`); without one,
    `planned(body)` names it from the request. `refusals` lists why each refused attempt was refused;
    `stopped` says why the spike stopped (a definitive error answer); `entries` are this run's attempts."""

    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        *,
        budget: Budget,
        spike: str,
        now: Clock,
        planned: Callable[[Mapping[str, Any]], str | None] | None = None,
    ) -> None:
        self._inner = inner
        self.budget = budget
        self.spike = spike
        self._now = now
        self._planned = planned
        self.case: str | None = None
        self.refusals: list[str] = []
        self.stopped: str | None = None
        self.entries: list[dict[str, Any]] = []
        self._started: set[str] = set()

    def counted_before(self, logical: str) -> bool:
        """Whether an earlier run counted the logical call `logical` (it is never sent again)."""
        return logical not in self._started and bool(self.budget.logical_entries(self.spike, logical))

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        method, path = request.method, request.url.path
        if method == "POST" and path in (MESSAGES_PATH, fake.BATCHES_PATH):
            return await self._paid(request, batch=path == fake.BATCHES_PATH)
        batches = path == fake.BATCHES_PATH or path.startswith(f"{fake.BATCHES_PATH}/")
        if (method == "POST" and path == COUNT_TOKENS_PATH) or (method == "GET" and batches):
            return await self._inner.handle_async_request(request)  # free: counted by no budget
        return self._refuse(f"{method} {path} is no route a spike sends")

    def _refuse(self, why: str) -> httpx2.Response:
        self.refusals.append(why)
        return refused_answer(why)

    async def _paid(self, request: httpx2.Request, *, batch: bool) -> httpx2.Response:
        try:
            body = json.loads(await request.aread())
            figures = batch_figures(body) if batch else message_figures(body)
        except (ValueError, KeyError, TypeError, AttributeError):
            return self._refuse("the request has no bound the budget can reserve")
        logical = self.case if self.case is not None else (self._planned(body) if self._planned else None)
        if logical is None:
            return self._refuse("the request is no call the spike planned")
        if self.stopped is not None:
            return self._refuse(f"the spike stopped: {self.stopped}")
        if self.counted_before(logical):
            return self._refuse(f"{logical} was counted by an earlier run and is never sent twice")
        if (why := self.budget.refusal_for(self.spike, figures.worst_case_usd)) is not None:
            return self._refuse(why)
        self._started.add(logical)
        attempt = len(self.budget.logical_entries(self.spike, logical)) + 1
        entry: dict[str, Any] = {
            "spike": self.spike,
            "case": f"{logical}#{attempt}",
            "logical_case": logical,
            "attempt": attempt,
            "purpose": request.headers.get(fake.PURPOSE_HEADER),
            "model": figures.model,
            "effort": figures.effort,
            "max_tokens": figures.max_tokens,
            "input_tokens_bound": figures.input_tokens_bound,
            "extra_usd": "0",
            "worst_case_usd": str(figures.worst_case_usd),
            "state": "sending",
            "sent_at": _iso(self._now()),
            "status": None,
            "error": None,
            "settled_usd": None,
            "settlement": None,
            "usage": None,
            "stop_reason": None,
        }
        if batch:
            entry |= {"batch": True, "items": figures.items}
        self.entries.append(self.budget.add(entry))  # counted before the send
        token = ATTEMPT_CASE.set(entry["case"])
        try:
            response = await self._inner.handle_async_request(request)
        except BaseException as exc:
            before = isinstance(exc, _BEFORE_SEND)
            self._done(
                entry,
                status=None,
                error=type(exc).__name__,
                settled=Decimal("0.000000") if before else figures.worst_case_usd,
                settlement="not_billed:connect_failed_before_send"
                if before
                else "ambiguous:kept_at_worst_case",
            )
            raise
        finally:
            ATTEMPT_CASE.reset(token)
        stream = response.stream
        if not isinstance(stream, httpx2.AsyncByteStream):
            await response.aclose()
            raise TypeError("the network transport answered with a sync stream")
        status, headers = response.status_code, response.headers

        def end(data: bytes, error: str | None) -> None:
            self._answered(entry, figures, status, headers, data, error, batch=batch)

        return httpx2.Response(
            status, headers=headers, stream=_SettlingStream(stream, end), extensions=response.extensions
        )

    def _answered(
        self,
        entry: dict[str, Any],
        figures: AttemptFigures,
        status: int,
        headers: Mapping[str, str],
        data: bytes,
        error: str | None,
        *,
        batch: bool,
    ) -> None:
        """Settle an answered attempt; a batch created stays open (`created`) until its results are read."""
        if batch and status == 200:
            try:
                batch_id = json.loads(data).get("id") if error is None else None
            except (ValueError, AttributeError):
                batch_id = None
            if isinstance(batch_id, str):
                entry.update(
                    state="created", status=status, provider_batch_id=batch_id, created_at=_iso(self._now())
                )
                self.budget.save()
                return
            self._done(entry, status, error, figures.worst_case_usd, "ambiguous:kept_at_worst_case")
        elif batch:
            settled = Decimal("0.000000") if status >= 400 else figures.worst_case_usd
            settlement = (
                "not_billed:error_response_no_content" if status >= 400 else "ambiguous:kept_at_worst_case"
            )
            self._done(entry, status, error, settled, settlement)
        else:
            s = settlement_of(
                figures.model, status, _events_of(status, headers, data), figures.worst_case_usd
            )
            self._done(
                entry, status, error, s.settled_usd, s.settlement, usage=s.usage, stop_reason=s.stop_reason
            )
        if status >= 400 and status not in RETRYABLE_STATUSES:
            self.stopped = f"{entry['case']} was answered http {status}"

    def _done(
        self,
        entry: dict[str, Any],
        status: int | None,
        error: str | None,
        settled: Decimal,
        settlement: str,
        *,
        usage: dict[str, int] | None = None,
        stop_reason: str | None = None,
    ) -> None:
        entry.update(
            state="done",
            status=status,
            error=error,
            settled_usd=str(settled),
            settlement=settlement,
            usage=usage,
            stop_reason=stop_reason,
            finished_at=_iso(self._now()),
        )
        self.budget.save()

    def finish(self) -> None:
        """Settle every attempt of this run whose answer never ended at its worst case (its outcome is
        unknown); a created batch stays open."""
        for entry in self.entries:
            if entry["state"] == "sending":
                worst = Decimal(entry["worst_case_usd"])
                self._done(entry, entry.get("status"), "unfinished", worst, "ambiguous:kept_at_worst_case")

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class Exchange:
    """One HTTP exchange as `TimingTransport` saw it: the request (its exact bytes, its headers without
    credentials) and the case `BudgetedTransport` counted it as; when it was handed to the network, when its
    headers arrived, each response chunk with its arrival time, and when it ended (all monotonic)."""

    method: str
    url: str
    path: str
    request_headers: dict[str, str]
    request_content: bytes = field(repr=False)
    case: str | None
    started: float
    headers_at: float | None = None
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    chunks: list[tuple[float, bytes]] = field(default_factory=list, repr=False)
    ended: float | None = None
    error: str | None = None
    operation: str | None = None  # the batch spike's name of the call that made it

    @property
    def logical_case(self) -> str | None:
        return None if self.case is None else self.case.rsplit("#", 1)[0]

    def request_body(self) -> dict[str, Any] | None:
        if not self.request_content:
            return None
        body = json.loads(self.request_content)
        return body if isinstance(body, dict) else None

    def body(self) -> bytes:
        return b"".join(chunk for _, chunk in self.chunks)

    @property
    def streamed(self) -> bool:
        return self.status == 200 and self.response_headers.get("content-type", "").startswith(
            "text/event-stream"
        )

    def events(self) -> list[fake.SSEEvent]:
        return _events_of(self.status, self.response_headers, self.body())

    def event_times(self) -> list[tuple[float, fake.SSEEvent]]:
        """Every SSE event with the arrival time of the chunk that completed it."""
        decoder = codecs.getincrementaldecoder("utf-8")()
        buffer, timed = "", []
        for at, chunk in self.chunks:
            buffer = (buffer + decoder.decode(chunk)).replace("\r\n", "\n")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                timed += [(at, event) for event in fake.parse_sse(f"{block}\n\n".encode())]
        return timed

    def max_gap_s(self) -> float | None:
        """The largest gap between two SSE events in a row; None with fewer than two."""
        times = [at for at, _ in self.event_times()]
        return max((b - a for a, b in itertools.pairwise(times)), default=None)


class _TimedStream(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream, exchange: Exchange, clock: Callable[[], float]) -> None:
        self._inner = inner
        self._exchange = exchange
        self._clock = clock

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._inner:
                self._exchange.chunks.append((self._clock(), bytes(chunk)))
                yield chunk
        except BaseException as exc:
            self._exchange.error = self._exchange.error or type(exc).__name__
            raise
        finally:
            if self._exchange.ended is None:
                self._exchange.ended = self._clock()

    async def aclose(self) -> None:
        if self._exchange.ended is None:
            self._exchange.ended = self._clock()
        await self._inner.aclose()


class TimingTransport(httpx2.AsyncBaseTransport):
    """Keeps every exchange it forwards (`exchanges`): the exact request body and every response chunk stamped
    with `clock` (module docstring). It asks for an identity encoding, so the chunks are the bytes the SDK
    parses."""

    def __init__(
        self, inner: httpx2.AsyncBaseTransport, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._inner = inner
        self._clock = clock
        self.exchanges: list[Exchange] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        content = await request.aread()
        request.headers["accept-encoding"] = "identity"
        exchange = Exchange(
            method=request.method,
            url=str(request.url),
            path=request.url.path,
            request_headers={
                k.lower(): v for k, v in request.headers.items() if k.lower() not in _SECRET_HEADERS
            },
            request_content=bytes(content),
            case=ATTEMPT_CASE.get(),
            started=self._clock(),
        )
        self.exchanges.append(exchange)
        try:
            response = await self._inner.handle_async_request(request)
        except BaseException as exc:
            exchange.error, exchange.ended = type(exc).__name__, self._clock()
            raise
        exchange.headers_at, exchange.status = self._clock(), response.status_code
        exchange.response_headers = {k.lower(): v for k, v in response.headers.items()}
        stream = response.stream
        if not isinstance(stream, httpx2.AsyncByteStream):
            raise TypeError("the network transport answered with a sync stream")
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_TimedStream(stream, exchange, self._clock),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


# --- what a call left behind -------------------------------------------------------------------------------


def _system_texts(body: Mapping[str, Any]) -> list[str]:
    system = body.get("system")
    if system is None:
        return []
    if isinstance(system, str):
        return [system]
    return [str(block.get("text", "")) for block in system]


def _message_texts(body: Mapping[str, Any]) -> list[str]:
    texts = []
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.append("".join(str(b.get("text", "")) for b in content if b.get("type") == "text"))
    return texts


def payload_problems(call: PlannedCall, captured: Mapping[str, Any] | None) -> list[str]:
    """How the captured request departs from the call's policy (§10.9), as names; [] when it does not."""
    if captured is None or captured.get("body") is None:
        return ["no request was captured"]
    body: Mapping[str, Any] = captured["body"]
    output_config = body.get("output_config", {})
    checks = {
        "host api.anthropic.com": captured["url"] == f"{ANTHROPIC_BASE_URL}/v1/messages",
        "stream true": body.get("stream") is True,
        "model": body.get("model") == call.model,
        "max_tokens": body.get("max_tokens") == call.max_tokens,
        "output_config.format json_schema": output_config.get("format", {}).get("type") == "json_schema",
        "output_config.format.schema": output_config.get("format", {}).get("schema")
        == call.transformed_schema,
        "output_config.effort": output_config.get("effort") == call.effort,
        "last message user": (body.get("messages") or [{}])[-1].get("role") == "user",
        "system as planned": _system_texts(body) == list(call.system),
        "purpose header": captured["headers"].get(fake.PURPOSE_HEADER.lower()) == call.purpose,
    }
    if isinstance(call.user, str):
        checks["user message as planned"] = _message_texts(body)[-1:] == [call.user]
    problems = [name for name, ok in checks.items() if not ok]
    problems += [f"{key} present" for key in BANNED_REQUEST_KEYS if key in body]
    return problems


def _merged_usage(events: list[fake.SSEEvent]) -> dict[str, int] | None:
    """The response's usage: `message_delta`'s cumulative counts, `message_start`'s for what it lacks."""
    start = next((e.data["message"].get("usage", {}) for e in events if e.name == "message_start"), {})
    delta = next((e.data.get("usage", {}) for e in events if e.name == "message_delta"), None)
    if delta is None:
        return None
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    return {k: int(delta.get(k) if delta.get(k) is not None else start.get(k) or 0) for k in keys}


@dataclass(frozen=True)
class Settled:
    """How one answered send settles: its cost and settlement, its usage, whether the stream completed, its
    stop reason."""

    settled_usd: Decimal
    settlement: str
    usage: dict[str, int] | None
    complete: bool
    stop_reason: str | None


def settlement_of(
    model: pricing.Model, status: int | None, events: list[fake.SSEEvent], worst_case: Decimal
) -> Settled:
    """A message's settlement (module docstring): from the recorded usage when the stream completed, $0 for an
    error response (no content), its worst case when the outcome is unknown."""
    usage = _merged_usage(events)
    complete = bool(events) and events[-1].name == "message_stop" and usage is not None
    stop_reason = next(
        (e.data["delta"].get("stop_reason") for e in events if e.name == "message_delta"), None
    )
    if complete and usage is not None:
        counts = pricing.Usage(
            usage["input_tokens"],
            usage["output_tokens"],
            usage["cache_creation_input_tokens"],
            usage["cache_read_input_tokens"],
        )
        return Settled(pricing.settled_usd(model, counts), "billed", usage, True, stop_reason)
    if status is not None and status >= 400:
        return Settled(Decimal("0.000000"), "not_billed:error_response_no_content", usage, False, stop_reason)
    return Settled(worst_case, "ambiguous:kept_at_worst_case", usage, False, stop_reason)


def _events_of(status: int | None, headers: Mapping[str, str], body: bytes) -> list[fake.SSEEvent]:
    """The SSE events of a streamed 200 answer ([] for anything else, or an unreadable stream)."""
    if status != 200 or not str(headers.get("content-type", "")).startswith("text/event-stream"):
        return []
    try:
        return fake.parse_sse(body)
    except ValueError:
        return []


def commit_stream(
    fixtures_out: Path,
    *,
    spike: str,
    case: str,
    purpose: str,
    model: str,
    effort: str,
    max_tokens: int,
    body: Mapping[str, Any],
    status: int | None,
    headers: Mapping[str, str],
    events: list[fake.SSEEvent],
    usage: dict[str, int],
    key: str,
    name: str | None = None,
    serves: str | None = None,
    native_pdf_count_tokens: int | None = None,
    input_tokens_bound: int | None = None,
) -> Path:
    """Commit one completed stream where the fake serves it (`<purpose>/<model>/<schema12>.sse`), or beside
    it as `<schema12>.<name>.sse`, with its `.headers.json` and `.components.json` sidecars (module
    docstring). `serves` puts it where the fake serves another purpose of the same model and schema (the
    sidecar keeps the purpose it was recorded for and names the one it serves)."""
    schema = body["output_config"]["format"]["schema"]
    stem = fake.schema12(schema) if name is None else f"{fake.schema12(schema)}.{name}"
    path = fake.recording_path(fixtures_out, serves or purpose, model, schema).with_name(f"{stem}.sse")
    stream = committed_stream(events, f"msg_recorded_{stem.replace('.', '_')}")
    committed = Tree.committed(fixtures_out)
    _atomic_write(committed, path, _without_key(fake.render_sse(stream), key))
    kept = {k: v for k, v in headers.items() if k.lower() in COMMITTED_HEADERS}
    _atomic_write(
        committed, path.with_suffix(".headers.json"), _json({"status": status, "headers": kept}, key)
    )
    components: dict[str, Any] = {
        "source": "recorded",
        "spike": spike,
        "case": case,
        "purpose": purpose,
        "model": model,
        "effort": effort,
        "max_tokens": max_tokens,
        "component_bytes": {
            "system": [len(t.encode("utf-8")) for t in _system_texts(body)],
            "messages": [len(t.encode("utf-8")) for t in _message_texts(body)],
            "schema": len(fake.canonical_schema_json(schema).encode("utf-8")),
        },
        "usage": usage,
    }
    if serves is not None:
        components["serves"] = serves
    if native_pdf_count_tokens is not None:
        components["native_pdf_count_tokens"] = native_pdf_count_tokens
        components["input_tokens_bound"] = input_tokens_bound
    _atomic_write(committed, path.with_suffix(".components.json"), _json(components, key))
    return path


def commit_error(
    fixtures_out: Path,
    *,
    purpose: str,
    model: str,
    request_body: Mapping[str, Any],
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    key: str,
) -> Path:
    """The fake's answer of an error response: its JSON body without the `request_id`, and its status and
    `content-type`; no sidecar (an error carries no usage)."""
    schema = request_body["output_config"]["format"]["schema"]
    path = fake.recording_path(fixtures_out, purpose, model, schema)
    error = json.loads(body)
    if not isinstance(error, dict):
        raise ValueError("an error response that is no JSON object")
    error.pop("request_id", None)
    committed = Tree.committed(fixtures_out)
    _atomic_write(committed, path, _json(error, key))
    kept = {k: v for k, v in headers.items() if k.lower() in COMMITTED_HEADERS}
    _atomic_write(
        committed, path.with_suffix(".headers.json"), _json({"status": status, "headers": kept}, key)
    )
    return path


def committed_stream(events: list[fake.SSEEvent], message_id: str) -> list[fake.SSEEvent]:
    """`events` without provider identity or thinking: the message id replaced, each thinking block reduced to
    one `fake.THINKING_OMITTED` delta and one `fake.SIGNATURE_OMITTED` signature."""
    thinking = {
        int(e.data["index"])
        for e in events
        if e.name == "content_block_start"
        and e.data["content_block"].get("type") in ("thinking", "redacted_thinking")
    }
    out: list[fake.SSEEvent] = []
    seen: set[tuple[int, str]] = set()
    for e in events:
        data = e.data
        if e.name == "message_start":
            data = {**data, "message": {**data["message"], "id": message_id}}
        elif e.name == "content_block_start" and int(data["index"]) in thinking:
            block = dict(data["content_block"])
            for name in ("signature", "data"):
                if name in block:
                    block[name] = fake.SIGNATURE_OMITTED
            if block.get("thinking"):
                block["thinking"] = fake.THINKING_OMITTED
            data = {**data, "content_block": block}
        elif e.name == "content_block_delta" and int(data["index"]) in thinking:
            kind = str(data["delta"].get("type"))
            if (int(data["index"]), kind) in seen:
                continue
            seen.add((int(data["index"]), kind))
            if kind == "thinking_delta":
                data = {**data, "delta": {"type": kind, "thinking": fake.THINKING_OMITTED}}
            elif kind == "signature_delta":
                data = {**data, "delta": {"type": kind, "signature": fake.SIGNATURE_OMITTED}}
        out.append(fake.SSEEvent(e.name, data))
    return out


# --- the gateway and batch spikes (M3 Task 26) -------------------------------------------------------------


@dataclass(frozen=True)
class GatewayCall:
    """One logical call a gateway spike plans (`gateway_calls(spike)`): its case, its node and its prompt."""

    case: str
    node: NodeConfig
    prompt: RenderedPrompt

    def input_tokens_bound(self) -> int:
        """The gateway's text bound of the call (`LLMGateway._input_bound`)."""
        return bounds.text_input_bound(self.node.model, self.prompt.as_request(self.node.schema))

    def worst_case_usd(self, *, enlarged: bool = False) -> Decimal:
        """What one attempt of the call reserves, at its first `max_tokens` stage or its enlarged one."""
        tokens = self.node.max_tokens_retry if enlarged else self.node.max_tokens
        return pricing.worst_case_usd(
            self.node.model, input_tokens_bound=self.input_tokens_bound(), max_tokens=tokens
        )


@dataclass(frozen=True)
class BatchItem:
    """One item a batch spike plans (`batch_requests(spike)`): its `custom_id`, its node and its prompt."""

    custom_id: str
    node: NodeConfig
    prompt: RenderedPrompt


def _checked_node(spike: str, case: str, node: object, prompt: object) -> tuple[NodeConfig, RenderedPrompt]:
    if not isinstance(node, NodeConfig) or not isinstance(prompt, RenderedPrompt):
        raise ValueError(f"{spike} {case}: a gateway call is a NodeConfig and a RenderedPrompt")
    if node.model not in pricing.MODELS or node.effort not in pricing.SUPPORTED_EFFORTS[node.model]:
        raise ValueError(f"{spike} {case}: model {node.model!r} with effort {node.effort!r} is not allowed")
    return node, prompt


def gateway_plan(spike: str, module: ModuleType) -> list[GatewayCall]:
    """The validated calls of `module.gateway_calls(spike)`."""
    calls = []
    for raw in module.gateway_calls(spike):
        node, prompt = _checked_node(spike, str(raw["case"]), raw["node"], raw["prompt"])
        calls.append(GatewayCall(str(raw["case"]), node, prompt))
    if len({c.case for c in calls}) != len(calls):
        raise ValueError(f"{spike}: case names must be unique")
    return calls


def batch_plan(spike: str, module: ModuleType) -> list[BatchItem]:
    """The validated items of `module.batch_requests(spike)`."""
    items = []
    for raw in module.batch_requests(spike):
        custom_id = str(raw["custom_id"])
        if not fake.CUSTOM_ID.fullmatch(custom_id):
            raise ValueError(f"{spike}: the custom_id {custom_id!r} is not one the API takes")
        node, prompt = _checked_node(spike, custom_id, raw["node"], raw["prompt"])
        items.append(BatchItem(custom_id, node, prompt))
    if not items or len({i.custom_id for i in items}) != len(items):
        raise ValueError(f"{spike}: a batch has items with unique custom_ids")
    if len({i.node.model for i in items}) != 1:
        raise ValueError(f"{spike}: a batch is of one model")
    return items


def _request_key(model: object, schema: object, system: Sequence[str], messages: Sequence[str]) -> str:
    return json.dumps([model, schema, list(system), list(messages)], sort_keys=True, ensure_ascii=False)


def planned_matcher(calls: Sequence[GatewayCall]) -> Callable[[Mapping[str, Any]], str | None]:
    """The case of the one planned call a request carries (its model, schema, system and messages, whatever
    its `max_tokens` stage), or None when it is none of them or more than one (a spike that sends the same
    request twice names its case, `GatewaySession.case`)."""
    cases: dict[str, list[str]] = {}
    for call in calls:
        rendered = call.prompt.as_request(call.node.schema)
        key = _request_key(call.node.model, dict(call.node.schema.json), rendered.system, rendered.messages)
        cases.setdefault(key, []).append(call.case)

    def match(body: Mapping[str, Any]) -> str | None:
        schema = ((body.get("output_config") or {}).get("format") or {}).get("schema")
        found = cases.get(
            _request_key(body.get("model"), schema, _system_texts(body), _message_texts(body)), []
        )
        return found[0] if len(found) == 1 else None

    return match


def _prompt_problems(node: NodeConfig, prompt: RenderedPrompt, body: Mapping[str, Any]) -> dict[str, bool]:
    """The checks a request (or a batch item's params) of `node` and `prompt` passes: its model, effort and
    schema, the system blocks and their cache markers, and user turns only, as planned."""
    output_config = body.get("output_config") or {}
    system = body.get("system")
    blocks = system if isinstance(system, list) else []
    marked = [i for i, block in enumerate(blocks) if isinstance(block, dict) and "cache_control" in block]
    messages = body.get("messages") or []
    return {
        "model": body.get("model") == node.model,
        "output_config.format json_schema": (output_config.get("format") or {}).get("type") == "json_schema",
        "output_config.format.schema": (output_config.get("format") or {}).get("schema")
        == dict(node.schema.json),
        "output_config.effort": output_config.get("effort") == node.effort,
        "system as planned": _system_texts(body) == [block.text for block in prompt.system],
        "cache markers as planned": marked == [i for i, block in enumerate(prompt.system) if block.cache]
        and json.dumps(body).count('"cache_control"') == len(marked),
        "user turns only": bool(messages) and all(m.get("role") == "user" for m in messages),
        "messages as planned": _message_texts(body) == list(prompt.as_request(node.schema).messages),
    }


def gateway_payload_problems(call: GatewayCall, exchange: Exchange | None) -> list[str]:
    """How the request one try of `call` sent departs from its plan and the policy (§10.9); [] when not."""
    body = None if exchange is None else exchange.request_body()
    if exchange is None or body is None:
        return ["no request was captured"]
    checks = {
        "host api.anthropic.com": exchange.url == f"{ANTHROPIC_BASE_URL}{MESSAGES_PATH}",
        "stream true": body.get("stream") is True,
        "max_tokens": body.get("max_tokens") == call.node.max_tokens,
        **_prompt_problems(call.node, call.prompt, body),
        "purpose header": exchange.request_headers.get(fake.PURPOSE_HEADER.lower()) == call.node.purpose,
        "no fake key header": fake.KEY_HEADER.lower() not in exchange.request_headers,
        "reserved as the gateway reserves": message_figures(body).worst_case_usd == call.worst_case_usd(),
    }
    problems = [name for name, ok in checks.items() if not ok]
    return problems + [f"{key} present" for key in BANNED_REQUEST_KEYS if key in body]


def batch_params_problems(item: BatchItem, params: Mapping[str, Any]) -> list[str]:
    """How a batch item's params depart from its plan: the online body without `stream`, its first stage."""
    checks = {
        "no stream": "stream" not in params,
        "max_tokens": params.get("max_tokens") == item.node.max_tokens,
        **_prompt_problems(item.node, item.prompt, params),
    }
    problems = [name for name, ok in checks.items() if not ok]
    return problems + [f"{key} present" for key in BANNED_REQUEST_KEYS if key in params]


async def _try_locally(cfg: AppConfig, call: GatewayCall) -> Exchange | None:
    """The exchange one try of `call` makes (`gateway._chat` on `to_messages`, as `LLMGateway.call` sends a
    try) on a transport that answers locally: nothing leaves the process."""
    timing = TimingTransport(_AnswersLocally())
    node = call.node
    async with make_http_client(cfg, mount=timing) as client:
        runnable = _chat(
            node.model,
            node.effort,
            node.max_tokens,
            node.schema.json,
            api_key=SecretStr(DRY_RUN_KEY),
            http_client=client,
            purpose=node.purpose,
        )
        # the local 400 is the expected answer: the request is what counts
        with contextlib.suppress(Exception):
            await runnable.ainvoke(to_messages(call.prompt))
    return timing.exchanges[0] if timing.exchanges else None


@asynccontextmanager
async def _unconnected_gateway(cfg: AppConfig, client: httpx2.AsyncClient) -> AsyncIterator[LLMGateway]:
    """A gateway for what needs no database (a batch item's params): its engine is never connected."""
    engine = create_async_engine("postgresql+psycopg://unused@127.0.0.1:1/unused")
    try:
        yield LLMGateway(engine, cfg, http_client=client, cache=CacheMonitor())
    finally:
        await engine.dispose()


@asynccontextmanager
async def throwaway_database() -> AsyncIterator[AsyncEngine]:
    """A fresh PostgreSQL container from `tests/support`'s harness (the digest-pinned image, the repository's
    init scripts, random role passwords in files), migrated and seeded; removed with its data at the end."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    # imported here: only a gateway spike needs a database, and the test harness is no backend module
    from jobhunter.db.migrate import run_migrate_async
    from jobhunter.db.session import make_engine

    from tests.support import pg

    workdir = Path(tempfile.mkdtemp(prefix="jobhunter-spike-db-"))
    server = await asyncio.to_thread(pg.start_pg_server, workdir)
    try:
        await run_migrate_async(pg.config_for(server, "migrate"))
        engine = make_engine(pg.config_for(server, "worker"), pool_size=4)
        try:
            yield engine
        finally:
            await engine.dispose()
    finally:
        await asyncio.to_thread(pg.remove_pg_server, server)
        shutil.rmtree(workdir, ignore_errors=True)


_ACTIVE_IDS: Final = (
    "SELECT prompt_schema_version_id, model_policy_id, execution_config_id FROM active_config WHERE id = 1"
)


@dataclass
class GatewaySession:
    """What a gateway spike's `run_gateway(spike, session)` runs with (module docstring): the gateway on the
    throwaway database and the budgeted, timed client, the planned calls, and the recorder's commits."""

    recorder: Any  # the `Recorder` (no string annotation: the spikes load this module without registering it)
    gateway: LLMGateway
    engine: AsyncEngine
    budgeted: BudgetedTransport
    timing: TimingTransport
    calls: list[GatewayCall]
    key: str = field(repr=False)

    @property
    def spike(self) -> str:
        return self.recorder.spike

    @contextmanager
    def case(self, name: str) -> Iterator[None]:
        """Every attempt sent inside counts as an attempt of the logical call `name`."""
        self.budgeted.case = name
        try:
            yield
        finally:
            self.budgeted.case = None

    def counted(self, name: str) -> bool:
        """Whether an earlier run counted the call `name`: it is never sent again."""
        return self.budgeted.counted_before(name)

    @property
    def refusals(self) -> list[str]:
        return self.budgeted.refusals

    @property
    def stopped(self) -> str | None:
        return self.budgeted.stopped

    def exchanges(self, case: str | None = None) -> list[Exchange]:
        """This run's exchanges (every one, or those of the logical call `case`), in order."""
        return [e for e in self.timing.exchanges if case is None or e.logical_case == case]

    def attempts(self, case: str) -> list[dict[str, Any]]:
        """This run's budget entries of the logical call `case`."""
        return [e for e in self.budgeted.entries if e.get("logical_case") == case]

    def say(self, line: str) -> None:
        self.recorder.say(f"{self.spike} {line}")

    async def active_ids(self) -> dict[str, int]:
        """The active prompt-schema version, model policy and execution config of the throwaway database."""
        async with self.engine.connect() as conn:
            row = (await conn.exec_driver_sql(_ACTIVE_IDS)).one()
        return {
            "prompt_schema_version_id": int(row.prompt_schema_version_id),
            "model_policy_id": int(row.model_policy_id),
            "execution_config_id": int(row.execution_config_id),
        }

    def commit_stream(
        self, exchange: Exchange, *, name: str | None = None, serves: str | None = None
    ) -> Path | None:
        """Commit `exchange`'s completed stream (module docstring): where the fake serves it (or serves the
        purpose `serves`), or beside it as `<schema12>.<name>.sse`; None (nothing written) for an answer that
        is no completed stream."""
        body, events = exchange.request_body(), exchange.events()
        usage = _merged_usage(events)
        if body is None or usage is None or not events or events[-1].name != "message_stop":
            self.say(f"{exchange.case} not committed: no completed stream")
            return None
        output_config = body["output_config"]
        return commit_stream(
            self.recorder.fixtures_out,
            spike=self.spike,
            case=exchange.logical_case or "",
            purpose=exchange.request_headers.get(fake.PURPOSE_HEADER.lower(), ""),
            model=str(body["model"]),
            effort=str(output_config.get("effort")),
            max_tokens=int(body["max_tokens"]),
            body=body,
            status=exchange.status,
            headers=exchange.response_headers,
            events=events,
            usage=usage,
            key=self.key,
            name=name,
            serves=serves,
        )

    def commit_answer(self, exchange: Exchange) -> Path | None:
        """Commit `exchange`'s answer where the fake serves it, whatever it was: a completed stream, or an
        error response (V4's rule, `RECORD_EVERY_ANSWER`)."""
        body = exchange.request_body()
        if exchange.streamed:
            return self.commit_stream(exchange)
        if body is None or exchange.status is None or exchange.status < 400:
            return None
        return commit_error(
            self.recorder.fixtures_out,
            purpose=exchange.request_headers.get(fake.PURPOSE_HEADER.lower(), ""),
            model=str(body["model"]),
            request_body=body,
            status=exchange.status,
            headers=exchange.response_headers,
            body=exchange.body(),
            key=self.key,
        )

    def commit_count(self, exchange: Exchange) -> Path | None:
        """Commit a `count_tokens` answer where the fake serves it (`fake.count_recording_path`): its status
        and JSON body without the `request_id`."""
        body = exchange.request_body()
        schema = ((body or {}).get("output_config") or {}).get("format", {}).get("schema")
        if body is None or exchange.status is None or not isinstance(schema, dict):
            return None
        answer = json.loads(exchange.body())
        answer = (
            {k: v for k, v in answer.items() if k != "request_id"} if isinstance(answer, dict) else answer
        )
        path = fake.count_recording_path(self.recorder.fixtures_out, str(body["model"]), schema)
        _atomic_write(
            Tree.committed(self.recorder.fixtures_out),
            path,
            _json({"status": exchange.status, "body": answer}, self.key),
        )
        return path


@contextmanager
def _tagged(timing: TimingTransport, operation: str) -> Iterator[None]:
    """Name every exchange made inside after the batch call `operation`."""
    before = len(timing.exchanges)
    try:
        yield
    finally:
        for exchange in timing.exchanges[before:]:
            exchange.operation = operation


def _exchange_stem(index: int, exchange: Exchange) -> str:
    name = exchange.case or exchange.path.rstrip("/").rsplit("/", 1)[-1]
    return f"{index:02d}-{name.replace('#', '-')}"


def _write_exchange(raw: Tree, directory: Path, stem: str, exchange: Exchange, key: str) -> None:
    """One exchange raw: its request (without credentials), its response headers and body, and its timing."""
    request = {
        "method": exchange.method,
        "url": exchange.url,
        "headers": exchange.request_headers,
        "body": exchange.request_body(),
    }
    _atomic_write(raw, directory / f"{stem}.request.json", _json(request, key))
    if exchange.status is not None:
        headers = {"status": exchange.status, "headers": dict(sorted(exchange.response_headers.items()))}
        _atomic_write(raw, directory / f"{stem}.response.headers.json", _json(headers, key))
        jsonl = exchange.path.endswith("/results")
        suffix = ".response.sse" if exchange.streamed else ".response.jsonl" if jsonl else ".response.json"
        _atomic_write(raw, directory / f"{stem}{suffix}", _without_key(exchange.body(), key))
    timing = {
        "started": exchange.started,
        "headers_at": exchange.headers_at,
        "ended": exchange.ended,
        "error": exchange.error,
        "chunks": [[at, len(chunk)] for at, chunk in exchange.chunks],
        "events": [[at, event.name] for at, event in exchange.event_times()] if exchange.streamed else [],
    }
    _atomic_write(raw, directory / f"{stem}.timing.json", _json(timing, key))


SANITISED_BATCH_ID: Final = "msgbatch_{spike}_recorded"


def _without_thinking(block: Any) -> Any:
    """A message content block without its thinking: a thinking block keeps its shape with
    `fake.THINKING_OMITTED` and `fake.SIGNATURE_OMITTED`, a redacted one `fake.SIGNATURE_OMITTED` as its
    data."""
    if not isinstance(block, dict):
        return block
    if block.get("type") == "thinking":
        thinking = fake.THINKING_OMITTED if block.get("thinking") else ""
        return {**block, "thinking": thinking, "signature": fake.SIGNATURE_OMITTED}
    if block.get("type") == "redacted_thinking":
        return {**block, "data": fake.SIGNATURE_OMITTED}
    return block


def _without_request_ids(doc: Any) -> Any:
    if isinstance(doc, dict):
        return {k: _without_request_ids(v) for k, v in doc.items() if k != "request_id"}
    if isinstance(doc, list):
        return [_without_request_ids(v) for v in doc]
    return doc


# --- one invocation ----------------------------------------------------------------------------------------


@dataclass
class Recorder:
    spike: str
    module: ModuleType
    calls: list[PlannedCall]
    root: Path
    fixtures_out: Path
    transport_factory: TransportFactory
    now: Clock
    out: TextIO
    gateway_calls: list[GatewayCall] = field(default_factory=list)
    batch_items: list[BatchItem] = field(default_factory=list)
    sleep: Sleep = asyncio.sleep
    database: DatabaseFactory = throwaway_database
    max_wait: timedelta = BATCH_MAX_WAIT
    _written: int = field(default=0, init=False, repr=False)

    def say(self, line: str) -> None:
        print(line, file=self.out, flush=True)

    async def dry_run(self) -> int:
        if self.spike in GATEWAY_SPIKES:
            return await self._dry_run_gateway()
        if self.spike in BATCH_SPIKES:
            return await self._dry_run_batch()
        budget = Budget(self.root)
        with tempfile.TemporaryDirectory(prefix="jobhunter-dry-run-") as no_secrets:
            cfg = load_config(
                "worker", {"JOBHUNTER_ENV": "dev", "JOBHUNTER_DB_NAME": "spike"}, Path(no_secrets)
            )
        code = OK
        for call in self.calls:
            worst = call.worst_case_usd()
            self.say(
                f"{call.spike} {call.case} {call.model} effort={call.effort} max_tokens={call.max_tokens} "
                f"input_tokens_bound={call.input_tokens_bound()} worst_case_usd={worst} "
                f"schema12={fake.schema12(call.transformed_schema)}"
            )
            if self.ladder is not None and not budget.has(call) and not self._wanted(call, budget):
                self.say(
                    f"{call.spike} {call.case} waits for the ladder: sent only when earlier verdicts ask"
                )
            if budget.has(call):
                self.say(f"{call.spike} {call.case} already counted: a live run skips it")
            elif (why := budget.refusal(call, worst)) is not None:
                self.say(f"{call.spike} {call.case} a live run would be REFUSED: {why}")
            else:
                budget.entries.append({"spike": call.spike, "case": call.case, "worst_case_usd": str(worst)})
            outcome = await _send(call, cfg, _AnswersLocally())
            problems = payload_problems(call, outcome.capture.request)
            self.say(
                f"{call.spike} {call.case} payload {'ok' if not problems else 'BAD: ' + ', '.join(problems)}"
            )
            if problems:
                code = STOPPED
        self.say(
            f"budget after a live run that sent every case: sent={budget.sent()} "
            f"committed_usd={budget.committed_usd()} of {MAX_ACTUAL_USD}"
        )
        return code

    @property
    def ladder(self) -> Callable[[str, Mapping[str, str]], bool] | None:
        """The spike's `wanted(case, verdicts)`, if it has one."""
        hook = getattr(self.module, "wanted", None)
        return hook if callable(hook) else None

    def verdicts(self, budget: Budget) -> dict[str, str]:
        """The `verdict` of each counted case of the spike whose check has run, from its raw
        `<case>.check.json`."""
        found: dict[str, str] = {}
        for call in self.calls:
            path = self.root / "anthropic" / call.spike / f"{call.case}.check.json"
            if budget.has(call) and path.is_file():
                verdict = json.loads(path.read_text("utf-8")).get("verdict")
                if isinstance(verdict, str):
                    found[call.case] = verdict
        return found

    def _wanted(self, call: PlannedCall, budget: Budget) -> bool:
        ladder = self.ladder
        return ladder is None or bool(ladder(call.case, self.verdicts(budget)))

    async def live(self, key: str) -> int:
        if self.spike in GATEWAY_SPIKES:
            return await self._live_gateway(key)
        if self.spike in BATCH_SPIKES:
            return await self._live_batch(key)
        with exclusive(self.root) as held:
            if not held:
                self.say(
                    f"{self.spike}: LOCKED, another recorder holds {self.root / 'anthropic-budget.lock'}"
                )
                return LOCKED
            budget = Budget(self.root)
            pending = []
            for call in self.calls:
                if budget.has(call):
                    self.say(f"{call.spike} {call.case} already counted; never sent twice")
                else:
                    pending.append(call)
            if not pending:
                self.say(f"{self.spike}: nothing left to send ({budget.left()})")
                return OK
            cfg = _worker_config(key)
            if cfg.secrets.get("anthropic_key") != key.encode("utf-8"):
                self.say(f"{self.spike}: FAILED, the anthropic_key secret did not load; nothing sent")
                return FAILED
            for call in pending:
                if not self._wanted(call, budget):
                    self.say(f"{call.spike} {call.case} not wanted by the spike's ladder; not sent")
                    continue
                worst = call.worst_case_usd()
                if (why := budget.refusal(call, worst)) is not None:
                    self.say(f"{call.spike} {call.case} REFUSED: {why}; nothing sent")
                    return REFUSED
                entry = budget.count(call, worst, self.now())
                outcome = await _send(call, cfg, self.transport_factory())
                if not self._finish(call, entry, budget, outcome, key):
                    self.say(f"{call.spike} {call.case} STOP: unexpected outcome; nothing more sent")
                    return STOPPED
            if self.ladder is not None:
                verdicts = self.verdicts(budget)
                self.say(f"{self.spike} verdicts: {', '.join(f'{c}={v}' for c, v in verdicts.items())}")
            return OK

    # --- the gateway spikes ---------------------------------------------------------------------------------

    def _dry_config(self) -> AppConfig:
        with tempfile.TemporaryDirectory(prefix="jobhunter-dry-run-") as no_secrets:
            return load_config(
                "worker", {"JOBHUNTER_ENV": "dev", "JOBHUNTER_DB_NAME": "spike"}, Path(no_secrets)
            )

    def _simulate(self, budget: Budget, logical: str, worst: Decimal) -> None:
        """The dry run's budget line: count `logical`'s one attempt as a live run would, or say why not."""
        if budget.logical_entries(self.spike, logical):
            self.say(f"{self.spike} {logical} already counted: a live run never sends it again")
        elif (why := budget.refusal_for(self.spike, worst)) is not None:
            self.say(f"{self.spike} {logical} a live run would be REFUSED: {why}")
        else:
            budget.entries.append(
                {
                    "spike": self.spike,
                    "case": f"{logical}#1",
                    "logical_case": logical,
                    "worst_case_usd": str(worst),
                }
            )

    def _budget_line(self, budget: Budget) -> None:
        self.say(
            f"budget after a live run that sent every case once: sent={budget.sent()} "
            f"committed_usd={budget.committed_usd()} of {MAX_ACTUAL_USD}"
        )

    async def _dry_run_gateway(self) -> int:
        budget = Budget(self.root)
        cfg = self._dry_config()
        code = OK
        for call in self.gateway_calls:
            node, worst = call.node, call.worst_case_usd()
            self.say(
                f"{self.spike} {call.case} {node.model} effort={node.effort} "
                f"max_tokens={node.max_tokens}/{node.max_tokens_retry} "
                f"input_tokens_bound={call.input_tokens_bound()} "
                f"worst_case_usd={worst} enlarged_worst_case_usd={call.worst_case_usd(enlarged=True)} "
                f"schema12={fake.schema12(node.schema.json)}"
            )
            self._simulate(budget, call.case, worst)
            problems = gateway_payload_problems(call, await _try_locally(cfg, call))
            self.say(
                f"{self.spike} {call.case} payload {'ok' if not problems else 'BAD: ' + ', '.join(problems)}"
            )
            if problems:
                code = STOPPED
        notes = getattr(self.module, "dry_run_notes", None)
        for line in notes(self.spike) if callable(notes) else []:
            self.say(f"{self.spike} {line}")
        first = sum((c.worst_case_usd() for c in self.gateway_calls), Decimal(0))
        costliest = max((c.worst_case_usd(enlarged=True) for c in self.gateway_calls), default=Decimal(0))
        self.say(
            f"{self.spike} worst case: ${first} for one attempt of each of its "
            f"{len(self.gateway_calls)} calls; "
            f"at most {SPIKE_CALLS[self.spike]} attempts in all, a retry at most ${costliest}"
        )
        self._budget_line(budget)
        return code

    async def _live_gateway(self, key: str) -> int:
        with exclusive(self.root) as held:
            if not held:
                self.say(
                    f"{self.spike}: LOCKED, another recorder holds {self.root / 'anthropic-budget.lock'}"
                )
                return LOCKED
            budget = Budget(self.root)
            if all(budget.logical_entries(self.spike, call.case) for call in self.gateway_calls):
                self.say(f"{self.spike}: nothing left to send ({budget.left()})")
                return OK
            cfg = _worker_config(key)
            if cfg.secrets.get("anthropic_key") != key.encode("utf-8"):
                self.say(f"{self.spike}: FAILED, the anthropic_key secret did not load; nothing sent")
                return FAILED
            report: dict[str, Any] = {}
            async with self.database() as engine:
                timing = TimingTransport(self.transport_factory())
                budgeted = BudgetedTransport(
                    timing,
                    budget=budget,
                    spike=self.spike,
                    now=self.now,
                    planned=planned_matcher(self.gateway_calls),
                )
                try:
                    async with make_http_client(cfg, mount=budgeted) as client:
                        gateway = LLMGateway(
                            engine, cfg, http_client=client, sleep=self.sleep, cache=CacheMonitor()
                        )
                        session = GatewaySession(
                            self, gateway, engine, budgeted, timing, self.gateway_calls, key
                        )
                        report = dict(await self.module.run_gateway(self.spike, session))
                finally:
                    budgeted.finish()
                    # each run its own directory: a later run (after a stop) never replaces what one measured
                    run_dir = self.root / "anthropic" / self.spike / f"run-{self.now():%Y%m%dT%H%M%SZ}"
                    for index, exchange in enumerate(timing.exchanges):
                        _write_exchange(
                            Tree.raw(self.root), run_dir, _exchange_stem(index, exchange), exchange, key
                        )
            report |= {"refusals": budgeted.refusals, "stopped": budgeted.stopped, "run": run_dir.name}
            for path in (run_dir / "report.json", self.root / "anthropic" / self.spike / "report.json"):
                _atomic_write(Tree.raw(self.root), path, _json(report, key))
            for line in report.get("lines", []):
                self.say(f"{self.spike} {line}")
            self.say(f"{self.spike}: {'done' if report.get('ok') else 'STOPPED'} ({budget.left()})")
            return OK if report.get("ok") else STOPPED

    # --- the batch spike -----------------------------------------------------------------------------------

    async def _batch_requests(self, cfg: AppConfig, client: httpx2.AsyncClient) -> list[BatchRequest]:
        """The items' create requests: each the online generator body without `stream` at its first stage
        (`LLMGateway.batch_request_body`, the cache marker kept)."""
        async with _unconnected_gateway(cfg, client) as gateway:
            return [
                BatchRequest(
                    item.custom_id,
                    gateway.batch_request_body(item.node, item.prompt, max_tokens=item.node.max_tokens),
                )
                for item in self.batch_items
            ]

    async def _dry_run_batch(self) -> int:
        budget = Budget(self.root)
        cfg = _worker_config(DRY_RUN_KEY)  # the params need a configured gateway; nothing is sent
        timing = TimingTransport(_AnswersLocally())
        code = OK
        async with make_http_client(cfg, mount=timing) as client:
            requests = await self._batch_requests(cfg, client)
            worst = Decimal(0)
            for item, request in zip(self.batch_items, requests, strict=True):
                figures = message_figures(request.params, batch=True)
                worst += figures.worst_case_usd
                self.say(
                    f"{self.spike} {item.custom_id} {figures.model} effort={figures.effort} "
                    f"max_tokens={figures.max_tokens} input_tokens_bound={figures.input_tokens_bound} "
                    f"worst_case_usd={figures.worst_case_usd} (batch prices) "
                    f"schema12={fake.schema12(item.node.schema.json)}"
                )
                problems = batch_params_problems(item, request.params)
                self.say(
                    f"{self.spike} {item.custom_id} payload "
                    f"{'ok' if not problems else 'BAD: ' + ', '.join(problems)}"
                )
                code = STOPPED if problems else code
            self.say(f"{self.spike} {BATCH_CASE} of {len(requests)} items worst_case_usd={worst}")
            self._simulate(budget, BATCH_CASE, worst)
            await BatchesClient(cfg, http_client=client).create(requests)  # answered 400 here: nothing leaves
        created = timing.exchanges[0].request_body() if timing.exchanges else None
        problems = [] if created is not None else ["no request was captured"]
        if created is not None:
            sent = [(r.get("custom_id"), r.get("params")) for r in created.get("requests", [])]
            if sent != [(r.custom_id, dict(r.params)) for r in requests]:
                problems.append("requests as planned")
            if batch_figures(created).worst_case_usd != worst:
                problems.append("reserved as the items")
            headers = timing.exchanges[0].request_headers
            if {fake.KEY_HEADER.lower(), fake.PURPOSE_HEADER.lower()} & set(headers):
                problems.append("a fake header")
        verdict = (
            "ok (answered 400 here: nothing left the process)"
            if not problems
            else "BAD: " + ", ".join(problems)
        )
        self.say(f"{self.spike} create payload {verdict}")
        self.say(
            f"{self.spike} the live run polls every {BATCH_POLL_S:.0f} s for up to "
            f"{self.max_wait.total_seconds() / 3600:g} h, then lists the workspace until it finds the "
            "batch"
        )
        self._budget_line(budget)
        return STOPPED if problems else code

    async def _live_batch(self, key: str) -> int:
        with exclusive(self.root) as held:
            if not held:
                self.say(
                    f"{self.spike}: LOCKED, another recorder holds {self.root / 'anthropic-budget.lock'}"
                )
                return LOCKED
            budget = Budget(self.root)
            counted = budget.logical_entries(self.spike, BATCH_CASE)
            entry = counted[-1] if counted else None
            if entry is not None and entry.get("state") == "done":
                self.say(f"{self.spike}: nothing left to send ({budget.left()})")
                return OK
            if entry is not None and entry.get("state") != "created":
                self.say(
                    f"{self.spike}: STOP, the create counted at {entry.get('sent_at')} has no known outcome "
                    "(the listing may find its batch); nothing sent"
                )
                return STOPPED
            cfg = _worker_config(key)
            if cfg.secrets.get("anthropic_key") != key.encode("utf-8"):
                self.say(f"{self.spike}: FAILED, the anthropic_key secret did not load; nothing sent")
                return FAILED
            timing = TimingTransport(self.transport_factory())
            budgeted = BudgetedTransport(
                timing, budget=budget, spike=self.spike, now=self.now, planned=lambda body: BATCH_CASE
            )
            results: list[ItemResult] | None = None
            found = False
            try:
                async with make_http_client(cfg, mount=budgeted) as http:
                    client = BatchesClient(cfg, http_client=http, sleep=self.sleep)
                    if entry is None:
                        outcome = await client.create(await self._batch_requests(cfg, http))
                        self._flush(timing.exchanges, key)  # the create's capture first: a resume reads it
                        entry = budgeted.entries[-1] if budgeted.entries else None
                        if (
                            not isinstance(outcome, Created)
                            or entry is None
                            or entry.get("state") != "created"
                        ):
                            self.say(
                                f"{self.spike} create {type(outcome).__name__}: STOP, nothing more sent "
                                f"({budget.left()})"
                            )
                            return STOPPED
                        self.say(
                            f"{self.spike} batch created with {outcome.request_counts.total} items "
                            f"({budget.left()})"
                        )
                    else:
                        self.say(f"{self.spike} resuming the created batch: it is never created again")
                    batch_id = str(entry["provider_batch_id"])
                    if not await self._poll(client, timing, batch_id, entry, key):
                        return STOPPED
                    with _tagged(timing, "results"):  # the SDK retrieves the batch first, for its results_url
                        stream = await client.results(batch_id)
                        results = [result async for result in stream]
                    with _tagged(timing, "list"):
                        found = await self._find(client, batch_id)
            except BatchApiError as exc:
                self.say(
                    f"{self.spike} {exc.operation} failed ({exc.error_class}, status {exc.status}): the "
                    "batch stays counted; run again to resume"
                )
                return STOPPED
            finally:
                budgeted.finish()
                self._flush(timing.exchanges, key)
            self._settle_batch(budget, entry, results)
            docs = self._commit_batch(key)
            check = getattr(self.module, "check_batch", None)
            report = {**(check(docs) if callable(check) else {}), "listed": found}
            _atomic_write(
                Tree.raw(self.root), self.root / "anthropic" / self.spike / "report.json", _json(report, key)
            )
            for line in report.get("lines", []):
                self.say(f"{self.spike} {line}")
            self.say(f"{self.spike} settled_usd={entry['settled_usd']} listed={found} ({budget.left()})")
            return OK if report.get("expected", True) and found else STOPPED

    async def _poll(
        self,
        client: BatchesClient,
        timing: TimingTransport,
        batch_id: str,
        entry: Mapping[str, Any],
        key: str,
    ) -> bool:
        """Retrieve the batch every `BATCH_POLL_S` until it ends or `max_wait` after its creation has passed
        (`--max-wait-hours`, at most `BATCH_MAX_WAIT`); whether it ended. Each retrieval is written raw at
        once."""
        created = datetime.fromisoformat(str(entry.get("created_at") or entry["sent_at"]))
        while True:
            with _tagged(timing, "retrieve"):
                status = await client.retrieve(batch_id)
            self._flush(timing.exchanges, key)
            counts = status.request_counts
            waited = (self.now() - created).total_seconds()
            self.say(
                f"{self.spike} retrieve after {waited:.0f} s: {status.processing_status} "
                f"processing={counts.processing} succeeded={counts.succeeded} errored={counts.errored}"
            )
            if status.processing_status == "ended":
                return True
            if self.now() >= created + self.max_wait:
                self.say(
                    f"{self.spike}: STOP, the batch has not ended {self.max_wait.total_seconds() / 3600:g} h "
                    "after its creation; it stays counted at its worst case; run again to resume"
                )
                return False
            await self.sleep(BATCH_POLL_S)

    async def _find(self, client: BatchesClient, batch_id: str) -> bool:
        """List the workspace until the batch is found (the listing is used for nothing else)."""
        listing = client.list_all()
        try:
            async for summary in listing:
                if summary.id == batch_id:
                    return True
        finally:
            await cast(Any, listing).aclose()
        return False

    def _recommit_batch(self) -> int:
        with exclusive(self.root) as held:
            if not held:
                self.say(
                    f"{self.spike}: LOCKED, another recorder holds {self.root / 'anthropic-budget.lock'}"
                )
                return LOCKED
            counted = Budget(self.root).logical_entries(self.spike, BATCH_CASE)
            if (
                not counted
                or counted[-1].get("state") != "done"
                or not (self._batch_dir() / "results.response.jsonl").is_file()
            ):
                self.say(
                    f"{self.spike}: REFUSED, no settled batch with its results to commit; nothing written"
                )
                return REFUSED
            docs = self._commit_batch("")
        self.say(f"{self.spike}: committed {', '.join(sorted(docs))} from the raw capture; nothing sent")
        return OK

    def _batch_dir(self) -> Path:
        return self.root / "anthropic" / self.spike

    def _flush(self, exchanges: Sequence[Exchange], key: str) -> None:
        """Write the exchanges of this run not written yet (`_write_batch_exchanges`)."""
        self._write_batch_exchanges(exchanges[self._written :], key)
        self._written = len(exchanges)

    def _write_batch_exchanges(self, exchanges: Sequence[Exchange], key: str) -> None:
        """The batch spike's exchanges raw, named by route so a resumed run adds to them: `create`,
        `retrieve-<n>`, `results` and `list-<n>`."""
        directory = self._batch_dir()
        retrieved = len(list(directory.glob("retrieve-*.request.json")))
        listed = len(list(directory.glob("list-*.request.json")))
        for exchange in exchanges:
            if exchange.method == "POST":
                stem = "create"
            elif exchange.operation == "results":
                stem = "results" if exchange.path.endswith("/results") else "results-retrieve"
            elif exchange.operation == "list":
                listed += 1
                stem = f"list-{listed}"
            else:
                retrieved += 1
                stem = f"retrieve-{retrieved}"
            _write_exchange(Tree.raw(self.root), directory, stem, exchange, key)

    def _settle_batch(
        self, budget: Budget, entry: dict[str, Any], results: Sequence[ItemResult] | None
    ) -> None:
        """The batch's settlement: each succeeded item at its batch cost from its usage, an errored, canceled
        or expired one $0, a missing one at its worst case."""
        created = json.loads((self._batch_dir() / "create.request.json").read_text("utf-8"))["body"]
        planned = {r["custom_id"]: message_figures(r["params"], batch=True) for r in created["requests"]}
        by_id = {result.custom_id: result for result in results or []}
        total, items = Decimal(0), {}
        for custom_id, figures in planned.items():
            result = by_id.get(custom_id)
            if result is None:
                total += figures.worst_case_usd
                items[custom_id] = {"result": "missing", "settled_usd": str(figures.worst_case_usd)}
            elif result.result_type == "succeeded" and result.message is not None:
                u = result.message.usage
                usage = pricing.Usage(
                    u.input_tokens,
                    u.output_tokens,
                    u.cache_creation_input_tokens or 0,
                    u.cache_read_input_tokens or 0,
                )
                cost = pricing.settled_usd(figures.model, usage, batch=True)
                total += cost
                items[custom_id] = {
                    "result": "succeeded",
                    "stop_reason": result.message.stop_reason,
                    "usage": {
                        "input_tokens": usage.input_tokens,
                        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                        "cache_read_input_tokens": usage.cache_read_input_tokens,
                        "output_tokens": usage.output_tokens,
                    },
                    "settled_usd": str(cost),
                }
            else:
                items[custom_id] = {
                    "result": result.result_type,
                    "error_type": result.error_type,
                    "settled_usd": "0.000000",
                }
        entry.update(
            state="done",
            status=200,
            settled_usd=str(total),
            settlement="billed",
            usage=items,
            finished_at=_iso(self.now()),
        )
        budget.save()

    def _commit_batch(self, key: str) -> dict[str, Any]:
        """The batch's documents without provider ids, committed under `batches/<spike>/` (module
        docstring)."""
        directory = self._batch_dir()

        def read(stem: str, suffix: str = ".response.json") -> Any:
            return json.loads((directory / f"{stem}{suffix}").read_text("utf-8"))

        create = read("create")
        batch_id = str(create["id"])
        retrieves = [
            read(f"retrieve-{n}") for n in range(1, len(list(directory.glob("retrieve-*.request.json"))) + 1)
        ]
        lines = [
            json.loads(line)
            for line in (directory / "results.response.jsonl").read_text("utf-8").splitlines()
            if line.strip()
        ]
        for line in lines:
            message = (line.get("result") or {}).get("message")
            if isinstance(message, dict):
                message["id"] = f"msg_{self.spike}_recorded_{line['custom_id']}"
                if isinstance(message.get("content"), list):  # thinking is never stored (G6)
                    message["content"] = [_without_thinking(block) for block in message["content"]]
        listed: list[Any] = []
        for n in range(1, len(list(directory.glob("list-*.request.json"))) + 1):
            listed += [b for b in read(f"list-{n}").get("data", []) if b.get("id") == batch_id]
        sanitised = SANITISED_BATCH_ID.format(spike=self.spike)
        docs: dict[str, Any] = {
            "create": create,
            "retrieve": retrieves,
            "results": lines,
            "list": {"data": listed[:1], "has_more": False, "first_id": batch_id, "last_id": batch_id},
        }
        docs = json.loads(json.dumps(_without_request_ids(docs)).replace(batch_id, sanitised))
        committed = Tree.committed(self.fixtures_out)
        for name, doc in docs.items():
            _atomic_write(
                committed, self.fixtures_out / "batches" / self.spike / f"{name}.json", _json(doc, key)
            )
        return docs

    def _finish(
        self, call: PlannedCall, entry: dict[str, Any], budget: Budget, outcome: Outcome, key: str
    ) -> bool:
        """Settle and record one sent call; whether it completed as expected."""
        capture = outcome.capture
        raw_dir = self.root / "anthropic" / call.spike
        raw = Tree.raw(self.root)
        if capture.request is not None:
            _atomic_write(raw, raw_dir / f"{call.case}.request.json", _json(capture.request, key))
        status = capture.status
        streamed = status == 200 and capture.headers.get("content-type", "").startswith("text/event-stream")
        events: list[fake.SSEEvent] = []
        if status is not None:
            headers = {"status": status, "headers": dict(sorted(capture.headers.items()))}
            _atomic_write(raw, raw_dir / f"{call.case}.response.headers.json", _json(headers, key))
            suffix = ".response.sse" if streamed else ".response.json"
            _atomic_write(raw, raw_dir / f"{call.case}{suffix}", _without_key(bytes(capture.body), key))
            events = _events_of(status, capture.headers, bytes(capture.body))
        figures = settlement_of(call.model, status, events, Decimal(entry["worst_case_usd"]))
        settled, settlement, usage = figures.settled_usd, figures.settlement, figures.usage
        complete, stop_reason = figures.complete, figures.stop_reason
        every_answer = self.records_every_answer
        error_answer = every_answer and not streamed and status is not None and status >= 400
        entry.update(
            state="done",
            status=status,
            error=outcome.error,
            settled_usd=str(settled),
            settlement=settlement,
            usage=usage,
            stop_reason=stop_reason,
            finished_at=_iso(self.now()),
        )
        budget.save()
        self.say(
            f"{call.spike} {call.case} {status if status is not None else '-'} stop_reason={stop_reason} "
            f"error={outcome.error} worst_case_usd={entry['worst_case_usd']} settled_usd={settled} "
            f"{budget.left()}"
        )
        if self.records_raw_only:
            return self._check_only(call, events, capture, outcome, complete=complete, key=key)
        if error_answer and status is not None and capture.request is not None:
            # The spike records every answer (V4): this error is the case's live result; the spike goes on.
            self._record_error(
                call, capture.request["body"], status, capture.headers, bytes(capture.body), key
            )
            return True
        if not complete or outcome.error is not None or usage is None:
            if every_answer:
                self.say(f"{call.spike} {call.case} no answer to record; the spike goes on")
                return True
            return False
        observation = self._observation(call, events, capture, outcome)
        report = self.module.check(observation)
        _atomic_write(raw, raw_dir / f"{call.case}.check.json", _json(report, key))
        self.say(f"{call.spike} {call.case} check missing={report.get('missing')}")
        # Only a turn that ended and parsed is the stored `ok` stream of the fake, unless the spike records
        # every answer.
        parsed = observation["parsed_ok"]
        if (stop_reason != "end_turn" or not parsed) and not every_answer:
            self.say(f"{call.spike} {call.case} not committed: stop_reason={stop_reason} parsed={parsed}")
            return False
        self._commit(call, events, capture, usage, key)
        return True

    @property
    def records_every_answer(self) -> bool:
        return getattr(self.module, "RECORD_EVERY_ANSWER", False) is True

    @property
    def records_raw_only(self) -> bool:
        return getattr(self.module, "RAW_ONLY", False) is True

    def _check_only(
        self,
        call: PlannedCall,
        events: list[fake.SSEEvent],
        capture: Capture,
        outcome: Outcome,
        *,
        complete: bool,
        key: str,
    ) -> bool:
        """A `RAW_ONLY` spike's answer: the spike checks a completed stream, whatever its stop reason, or an
        error response. Its `expected` flag says whether the spike goes on. Nothing is committed."""
        status = capture.status
        if complete:
            observation = self._observation(call, events, capture, outcome)
        elif status is not None and status >= 400 and capture.request is not None:
            observation = self._error_observation(call, capture.request["body"], status, bytes(capture.body))
        else:
            self.say(f"{call.spike} {call.case} no answer to check")
            return False
        report = self.module.check(observation)
        path = self.root / "anthropic" / call.spike / f"{call.case}.check.json"
        _atomic_write(Tree.raw(self.root), path, _json(report, key))
        self.say(
            f"{call.spike} {call.case} verdict={report.get('verdict')} missing={report.get('missing')} "
            "(raw only: nothing committed)"
        )
        return report.get("expected") is True

    def _record_error(
        self,
        call: PlannedCall,
        request_body: Mapping[str, Any],
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        key: str,
    ) -> None:
        """Check an error answer and commit it as the fake's answer of the call."""
        report = self.module.check(self._error_observation(call, request_body, status, body))
        check = self.root / "anthropic" / call.spike / f"{call.case}.check.json"
        _atomic_write(Tree.raw(self.root), check, _json(report, key))
        self.say(f"{call.spike} {call.case} answer {status} recorded; check missing={report.get('missing')}")
        self._commit_error(call, request_body, status, headers, body, key)

    def commit_recorded(self) -> int:
        """`--commit-recorded`: commit the fake's answer of each counted case of a spike that records every
        answer, whose raw capture is an error response; for the batch spike, its documents again from the raw
        capture of its settled batch. Nothing is sent and no key is read."""
        if self.spike in BATCH_SPIKES:
            return self._recommit_batch()
        if not self.records_every_answer:
            self.say(f"{self.spike}: REFUSED, the spike does not record every answer; nothing written")
            return REFUSED
        with exclusive(self.root) as held:
            if not held:
                self.say(
                    f"{self.spike}: LOCKED, another recorder holds {self.root / 'anthropic-budget.lock'}"
                )
                return LOCKED
            budget = Budget(self.root)
            raw_dir = self.root / "anthropic" / self.spike
            for call in self.calls:
                entry = budget.entry(call) or {}
                status = entry.get("status")
                if entry.get("state") != "done" or not isinstance(status, int) or status < 400:
                    continue
                request = json.loads((raw_dir / f"{call.case}.request.json").read_text("utf-8"))
                response = json.loads((raw_dir / f"{call.case}.response.headers.json").read_text("utf-8"))
                body = (raw_dir / f"{call.case}.response.json").read_bytes()
                self._record_error(call, request["body"], status, response["headers"], body, "")
            return OK

    def _commit(
        self,
        call: PlannedCall,
        events: list[fake.SSEEvent],
        capture: Capture,
        usage: dict[str, int],
        key: str,
    ) -> None:
        body: Mapping[str, Any] = capture.request["body"] if capture.request else {}
        native = call.native_pdf_count_tokens
        commit_stream(
            self.fixtures_out,
            spike=call.spike,
            case=call.case,
            purpose=call.purpose,
            model=call.model,
            effort=call.effort,
            max_tokens=call.max_tokens,
            body=body,
            status=capture.status,
            headers=capture.headers,
            events=events,
            usage=usage,
            key=key,
            native_pdf_count_tokens=native,
            input_tokens_bound=None if native is None else call.input_tokens_bound(),
        )

    def _commit_error(
        self,
        call: PlannedCall,
        request_body: Mapping[str, Any],
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        key: str,
    ) -> None:
        """The fake's answer of an error response (`commit_error`)."""
        commit_error(
            self.fixtures_out,
            purpose=call.purpose,
            model=call.model,
            request_body=request_body,
            status=status,
            headers=headers,
            body=body,
            key=key,
        )

    @staticmethod
    def _error_observation(
        call: PlannedCall, request_body: Mapping[str, Any], status: int, body: bytes
    ) -> dict[str, Any]:
        try:
            error = json.loads(body)
        except ValueError:
            error = None
        detail = error.get("error", {}) if isinstance(error, dict) else {}
        return {
            "requested_model": call.model,
            "transformed_schema": call.transformed_schema,
            "request": request_body,
            "status": status,
            "error_type": detail.get("type"),
            "error_message": detail.get("message"),
        }

    @staticmethod
    def _observation(
        call: PlannedCall, events: list[fake.SSEEvent], capture: Capture, outcome: Outcome
    ) -> dict[str, Any]:
        out = outcome.out or {}
        raw = out.get("raw")
        parsing_error = out.get("parsing_error")
        return {
            "requested_model": call.model,
            "transformed_schema": call.transformed_schema,
            "request": capture.request["body"] if capture.request else {},
            "status": capture.status,
            "events": [{"name": e.name, "data": e.data} for e in events],
            "response_metadata": dict(raw.response_metadata) if isinstance(raw, AIMessage) else {},
            "usage_metadata": dict(raw.usage_metadata or {}) if isinstance(raw, AIMessage) else {},
            "parsed_ok": out.get("parsed") is not None,
            "parsing_error": None if parsing_error is None else type(parsing_error).__name__,
        }


def _json(doc: Any, key: str) -> bytes:
    return _without_key(
        (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode("utf-8"),
        key,
    )


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="record_anthropic.py", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--spike", required=True, choices=sorted(SPIKE_CALLS))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--commit-recorded", action="store_true")
    parser.add_argument(
        "--through-gateway",
        action="store_true",
        help="the gateway spikes' mode (v4v2, v5, v14), which they always run in; refused for any other",
    )
    parser.add_argument(
        "--max-wait-hours",
        type=float,
        default=None,
        help="the batch spike's longest wait for its batch to end, after its creation (at most 24)",
    )
    return parser.parse_args(argv)


async def _main(
    argv: Sequence[str] | None,
    *,
    environ: Mapping[str, str] | None,
    root: Path | None,
    fixtures_out: Path | None,
    transport_factory: TransportFactory | None,
    stream: TextIO,
    now: Clock | None,
    sleep: Sleep | None,
    database: DatabaseFactory | None,
) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    env = os.environ if environ is None else environ
    if traced := [name for name in TRACING_VARS if name in env]:  # their names only: a key is a value
        print(
            f"{args.spike}: REFUSED, LangSmith tracing could export the prompts; unset "
            f"{', '.join(traced)}; nothing sent",
            file=stream,
        )
        return REFUSED
    if args.through_gateway and args.spike not in GATEWAY_SPIKES:
        print(
            f"{args.spike}: REFUSED, --through-gateway is for v4v2, v5 and v14 only; nothing sent",
            file=stream,
        )
        return REFUSED
    hours = args.max_wait_hours
    if hours is not None and (args.spike not in BATCH_SPIKES or not 0 < hours <= 24):
        print(
            f"{args.spike}: REFUSED, --max-wait-hours is for v12, above 0 and at most 24; nothing sent",
            file=stream,
        )
        return REFUSED
    if args.commit_recorded and args.spike in GATEWAY_SPIKES:
        print(
            f"{args.spike}: REFUSED, --commit-recorded is for the message and batch spikes; nothing written",
            file=stream,
        )
        return REFUSED
    module = load_spike(args.spike)
    if module is None:
        print(
            f"{args.spike}: REFUSED, the spike has no definition ({SPIKE_MODULES[args.spike]}.py); "
            "nothing sent",
            file=stream,
        )
        return REFUSED
    calls: list[PlannedCall] = []
    gateway_calls: list[GatewayCall] = []
    batch_items: list[BatchItem] = []
    if args.spike in GATEWAY_SPIKES:
        gateway_calls = gateway_plan(args.spike, module)
        planned = len(gateway_calls)
    elif args.spike in BATCH_SPIKES:
        batch_items = batch_plan(args.spike, module)
        planned = 1  # one batch
    else:
        calls = plan(args.spike, module)
        planned = len(calls)
    if planned > SPIKE_CALLS[args.spike]:
        print(
            f"{args.spike}: REFUSED, {planned} calls planned but the spike may send "
            f"{SPIKE_CALLS[args.spike]}; "
            "nothing sent",
            file=stream,
        )
        return REFUSED
    recorder = Recorder(
        args.spike,
        module,
        calls,
        DEFAULT_ROOT if root is None else root,
        DEFAULT_FIXTURES if fixtures_out is None else fixtures_out,
        transport_factory or httpx2.AsyncHTTPTransport,
        now or (lambda: datetime.now(UTC)),
        stream,
        gateway_calls=gateway_calls,
        batch_items=batch_items,
        sleep=asyncio.sleep if sleep is None else sleep,
        database=throwaway_database if database is None else database,
        max_wait=BATCH_MAX_WAIT if hours is None else timedelta(hours=hours),
    )
    if args.dry_run:
        return await recorder.dry_run()
    if args.commit_recorded:
        return recorder.commit_recorded()
    key = env.get(KEY_VAR, "").strip()
    if not key:
        print(f"{args.spike}: REFUSED, {KEY_VAR} is not set; nothing sent", file=stream)
        return REFUSED
    register_secret_values([key])  # from here on a log line masks it
    return await recorder.live(key)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    root: Path | None = None,
    fixtures_out: Path | None = None,
    transport_factory: TransportFactory | None = None,
    out: TextIO | None = None,
    now: Clock | None = None,
    sleep: Sleep | None = None,
    database: DatabaseFactory | None = None,
) -> int:
    """Run one spike; the exit status is `OK`, `FAILED`, `LOCKED`, `REFUSED` or `STOPPED`. `root`,
    `fixtures_out`, `transport_factory`, `sleep` and `database` (tests only) replace the repository
    directories, the network, the waits and the throwaway database."""
    configure_logging()  # it also holds the SDK's and httpx2's loggers at WARNING
    stream = sys.stdout if out is None else out
    run = _main(
        argv,
        environ=environ,
        root=root,
        fixtures_out=fixtures_out,
        transport_factory=transport_factory,
        stream=stream,
        now=now,
        sleep=sleep,
        database=database,
    )
    try:
        return asyncio.run(run)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - the class only: an exception's text can carry request data
        print(f"record_anthropic: {type(exc).__name__}", file=stream)
        return FAILED


async def main_async(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    root: Path | None = None,
    fixtures_out: Path | None = None,
    transport_factory: TransportFactory | None = None,
    out: TextIO | None = None,
    now: Clock | None = None,
    sleep: Sleep | None = None,
    database: DatabaseFactory | None = None,
) -> int:
    """`main` inside a running event loop (tests only: a test on the shared database owns its loop)."""
    configure_logging()
    stream = sys.stdout if out is None else out
    try:
        return await _main(
            argv,
            environ=environ,
            root=root,
            fixtures_out=fixtures_out,
            transport_factory=transport_factory,
            stream=stream,
            now=now,
            sleep=sleep,
            database=database,
        )
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - the class only: an exception's text can carry request data
        print(f"record_anthropic: {type(exc).__name__}", file=stream)
        return FAILED


if __name__ == "__main__":
    # The SDK and langchain read ANTHROPIC_* variables (a base URL, a key, custom headers): none may steer a
    # paid call, so this process drops them before anything reads them (G1).
    for name in [name for name in os.environ if name.startswith("ANTHROPIC_")]:
        del os.environ[name]
    raise SystemExit(main())
