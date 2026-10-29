"""Spike V12 (M3 Task 26, spec §22 L6664): a 3-item Message Batch on `claude-haiku-5-5` with the real
generator schema C, and its committed documents replayed through the batch item decision.

`scripts/spikes/v12_batches.py` plans the items (`batch_requests("v12")`); the budgeted recorder sends the
create through `BatchesClient`, polls `retrieve`, streams the results, lists the workspace once and commits
the four documents, without provider identity, under `tests/contract/fixtures/anthropic/batches/v12/`:
`create.json`, `retrieve.json` (each retrieval in order), `results.json` (the result lines) and `list.json`
(the listing reduced to the spike's batch).

Until the live documents exist, every test here runs on documents the same recorder path writes on the fake
Batches API (`fake.RecordedSSETransport`, one item scripted `errored:invalid_request` so the not-billed path
is exercised too). The replay maps the recorded lines onto a submission of three synthetic targets (on a
tuple whose generator is Haiku, the batch's model; a submission takes at least 200) and runs
`batch_items.process_item` on each: usage present,
the `custom_id` pattern, the listing's `created_at` and `request_counts`, and every errored, canceled or
expired item settled `not_billed`. An `expired` item cannot be produced on demand (A3 G27); the expiry is
Task 17's fake's.
"""

import copy
import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import httpx2
import pytest
import sqlalchemy as sa
from anthropic.types.messages import MessageBatch, MessageBatchIndividualResponse
from jobhunter.clock import utcnow
from jobhunter.db.seed import canonical_json, load_settings_v1
from jobhunter.llm import batches as llm_batches
from jobhunter.llm import fake, pricing
from jobhunter.llm.fake import BatchScript
from jobhunter.scoring import batch_items
from jobhunter.types import get_object
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import entries_by_custom_id, item_ids, items_of, submit_batch
from tests.support.llm import FAKE_LLM_DIR, generator_wire_body, synthetic_sse

REPO = Path(__file__).resolve().parents[2]
RECORDED: Final = FAKE_LLM_DIR / "batches" / "v12"
DOCUMENTS: Final = ("create", "retrieve", "results", "list")
HAIKU: Final = "claude-haiku-5-5"
BATCH_MIN_TARGETS: Final = 200  # `scoring.batch_min_targets`, the seeded default and its floor
CUSTOM_ID: Final = re.compile(r"t\d+_a1")
FAKE_KEY: Final = "fake-anthropic-key-0001"  # visibly fake: the fake ignores it, nothing leaves the process
_EVIDENCE: Final = {
    "errored": "batch_item_errored",
    "canceled": "batch_item_canceled",
    "expired": "batch_item_expired",
}


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts/spikes" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rec = _load("record_anthropic")
V12 = _load("v12_batches")


@dataclass(frozen=True)
class Documents:
    create: dict[str, Any]
    retrieves: list[dict[str, Any]]
    results: list[dict[str, Any]]
    listing: dict[str, Any]
    recorded: bool


def _read(directory: Path) -> Documents:
    docs = {name: json.loads((directory / f"{name}.json").read_text("utf-8")) for name in DOCUMENTS}
    return Documents(docs["create"], docs["retrieve"], docs["results"], docs["list"], directory == RECORDED)


def _no_sleep() -> Any:
    async def sleep(seconds: float) -> None:
        return None

    return sleep


async def fake_run(tmp: Path) -> tuple[int, Path]:
    """The recorder's live V12 path on the fake Batches API: three items over a synthetic C answer on Haiku,
    the third scripted `errored:invalid_request`, every poll without a wait."""
    answers = tmp / "fake-anthropic"
    for item in V12.batch_requests("v12"):
        schema = item["node"].schema.json
        path = fake.recording_path(answers, "generate", HAIKU, schema)
        path.parent.mkdir(parents=True, exist_ok=True)
        usage = {"input_tokens": 1_900, "cache_creation_input_tokens": 5_200, "output_tokens": 700}
        path.write_bytes(synthetic_sse(generator_wire_body(), model=HAIKU, usage=usage))
    fake.batches(BatchScript(items={"t3_a1": "errored:invalid_request"}))
    code = await rec.main_async(
        ["--spike", "v12", "--live"],
        root=tmp / "raw",
        fixtures_out=tmp / "fixtures",
        environ={"JOBHUNTER_ANTHROPIC_API_KEY": FAKE_KEY},
        transport_factory=lambda: fake.RecordedSSETransport(answers),
        sleep=_no_sleep(),
    )
    return code, tmp / "fixtures" / "batches" / "v12"


@pytest.fixture
async def documents(tmp_path: Path) -> Documents:
    """The recorded V12 documents once they are committed; until then those the recorder writes on the
    fake."""
    if all((RECORDED / f"{name}.json").is_file() for name in DOCUMENTS):
        return _read(RECORDED)
    code, directory = await fake_run(tmp_path)
    assert code == rec.OK
    return _read(directory)


# --- the documents -----------------------------------------------------------------------------------------


async def test_the_items_have_the_custom_id_pattern_and_every_answered_item_its_usage(
    documents: Documents,
) -> None:
    lines = [MessageBatchIndividualResponse.model_validate(line) for line in documents.results]
    assert len(lines) == 3 and all(CUSTOM_ID.fullmatch(line.custom_id) for line in lines)
    assert sorted(line.custom_id for line in lines) == ["t1_a1", "t2_a1", "t3_a1"]
    for line in lines:
        if line.result.type == "succeeded":
            usage = line.result.message.usage
            assert usage.input_tokens > 0 and usage.output_tokens > 0
            assert usage.cache_creation_input_tokens is not None and usage.cache_read_input_tokens is not None
            assert line.result.message.model == HAIKU


async def test_the_listing_names_the_batch_with_its_created_at_and_request_counts(
    documents: Documents,
) -> None:
    (listed,) = documents.listing["data"]  # the spike's batch only: an empty listing proves nothing (C3)
    batch = MessageBatch.model_validate(listed)
    assert batch.id == documents.create["id"] and batch.created_at is not None
    counts = batch.request_counts
    assert (
        counts.succeeded + counts.errored + counts.canceled + counts.expired == 3 and counts.processing == 0
    )
    assert set(listed["request_counts"]) == {"processing", "succeeded", "errored", "canceled", "expired"}


async def test_the_create_and_the_retrievals_are_batches_until_the_last_one_ended(
    documents: Documents,
) -> None:
    created = MessageBatch.model_validate(documents.create)
    assert created.request_counts.processing == 3
    retrieved = [MessageBatch.model_validate(doc) for doc in documents.retrieves]
    assert retrieved and retrieved[-1].processing_status == "ended"
    assert all(r.processing_status != "ended" for r in retrieved[:-1])
    assert {r.id for r in retrieved} == {created.id}


async def test_no_document_keeps_a_provider_id(documents: Documents) -> None:
    text = json.dumps([documents.create, documents.retrieves, documents.results, documents.listing])
    ids = set(re.findall(r"msg(?:batch)?_[A-Za-z0-9_]+", text))
    assert ids and all(found.startswith(("msgbatch_v12_recorded", "msg_v12_recorded_t")) for found in ids), (
        ids
    )
    assert "request_id" not in text


async def test_no_document_keeps_thinking(documents: Documents) -> None:
    """Thinking content is never stored (G6): a thinking block keeps its shape only."""
    blocks = [
        block
        for line in documents.results
        for block in ((line["result"].get("message") or {}).get("content") or [])
        if block.get("type") in ("thinking", "redacted_thinking")
    ]
    for block in blocks:
        assert block.get("thinking", "") in ("", fake.THINKING_OMITTED)
        assert block.get("signature", fake.SIGNATURE_OMITTED) == fake.SIGNATURE_OMITTED
        assert block.get("data", fake.SIGNATURE_OMITTED) == fake.SIGNATURE_OMITTED


# --- the recorder's batch path, on the fake -------------------------------------------------------------


async def test_the_recorder_settles_the_batch_from_its_results_and_never_creates_it_twice(
    tmp_path: Path,
) -> None:
    code, directory = await fake_run(tmp_path)
    assert code == rec.OK
    (entry,) = json.loads((tmp_path / "raw/anthropic-budget.json").read_text("utf-8"))["entries"]
    assert (entry["spike"], entry["case"], entry["state"], entry["settlement"]) == (
        "v12",
        "batch#1",
        "done",
        "billed",
    )
    lines = json.loads((directory / "results.json").read_text("utf-8"))
    expected = sum(
        (
            pricing.settled_usd(
                HAIKU,
                pricing.Usage(
                    u["input_tokens"],
                    u["output_tokens"],
                    u["cache_creation_input_tokens"] or 0,
                    u["cache_read_input_tokens"] or 0,
                ),
                batch=True,
            )
            for u in (
                line["result"]["message"]["usage"] for line in lines if line["result"]["type"] == "succeeded"
            )
        ),
        Decimal(0),
    )
    assert Decimal(entry["settled_usd"]) == expected > 0  # the errored item is $0
    assert Decimal(entry["worst_case_usd"]) == sum(
        (
            pricing.worst_case_usd(
                HAIKU,
                input_tokens_bound=rec.message_figures(params, batch=True).input_tokens_bound,
                max_tokens=params["max_tokens"],
                batch=True,
            )
            for params in (r["params"] for r in _raw_create(tmp_path)["requests"])
        ),
        Decimal(0),
    )
    # a second run sends nothing: the batch is settled
    again = await rec.main_async(
        ["--spike", "v12", "--live"],
        root=tmp_path / "raw",
        fixtures_out=tmp_path / "fixtures",
        environ={"JOBHUNTER_ANTHROPIC_API_KEY": FAKE_KEY},
        transport_factory=lambda: (_ for _ in ()).throw(AssertionError("nothing is sent twice")),
        sleep=_no_sleep(),
    )
    assert again == rec.OK
    assert not any(FAKE_KEY in text for text in _every_text(tmp_path))


def _every_text(root: Path) -> list[str]:
    return [p.read_text("utf-8", errors="replace") for p in root.rglob("*") if p.is_file()]


class Crash(Exception):
    """A crash of the recorder during its poll (the test's sleep raises it)."""


def _fake_answers(tmp: Path) -> Path:
    answers = tmp / "fake-anthropic"
    for item in V12.batch_requests("v12"):
        path = fake.recording_path(answers, "generate", HAIKU, item["node"].schema.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(synthetic_sse(generator_wire_body(), model=HAIKU))
    return answers


async def test_a_run_after_a_crash_resumes_polling_the_created_batch_and_never_creates_another(
    tmp_path: Path,
) -> None:
    answers = _fake_answers(tmp_path)
    fake.batches(BatchScript(polls_until_ended=3))
    posts: list[str] = []

    def network() -> httpx2.AsyncBaseTransport:
        inner = fake.RecordedSSETransport(answers)

        class Counting(httpx2.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
                if request.method == "POST":
                    posts.append(request.url.path)
                return await inner.handle_async_request(request)

        return Counting()

    async def crash(seconds: float) -> None:
        raise Crash()

    common: dict[str, Any] = {
        "root": tmp_path / "raw",
        "fixtures_out": tmp_path / "fixtures",
        "environ": {"JOBHUNTER_ANTHROPIC_API_KEY": FAKE_KEY},
        "transport_factory": network,
    }
    assert await rec.main_async(["--spike", "v12", "--live"], sleep=crash, **common) == rec.FAILED
    (entry,) = json.loads((tmp_path / "raw/anthropic-budget.json").read_text("utf-8"))["entries"]
    assert (entry["state"], entry["provider_batch_id"], entry["settled_usd"]) == (
        "created",
        "msgbatch_1",
        None,
    )
    assert rec.Budget(tmp_path / "raw").unsettled_worst_case_usd() == Decimal(entry["worst_case_usd"])
    assert posts == ["/v1/messages/batches"]
    # the next run polls the same batch to its end and settles it; nothing is created again
    assert await rec.main_async(["--spike", "v12", "--live"], sleep=_no_sleep(), **common) == rec.OK
    assert posts == ["/v1/messages/batches"] and len(fake.state().batches) == 1
    (entry,) = json.loads((tmp_path / "raw/anthropic-budget.json").read_text("utf-8"))["entries"]
    assert (entry["state"], entry["settlement"]) == ("done", "billed")
    retrieves = json.loads((tmp_path / "fixtures/batches/v12/retrieve.json").read_text("utf-8"))
    assert [r["processing_status"] for r in retrieves] == [
        "in_progress",
        "in_progress",
        "in_progress",
        "ended",
    ]


async def test_a_batch_that_has_not_ended_within_the_wait_stops_and_stays_counted(tmp_path: Path) -> None:
    """`--max-wait-hours 2`: the poll stops two hours after the creation with the batch still `created`
    (counted at its worst case, for a later run to resume), and every exchange is on disk while it waits."""
    answers = _fake_answers(tmp_path)
    fake.batches(BatchScript(polls_until_ended=1_000))
    start = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    ticks: list[datetime] = []

    def clock() -> datetime:  # every reading half an hour after the previous one
        ticks.append(start + timedelta(minutes=30 * len(ticks)))
        return ticks[-1]

    raw = tmp_path / "raw/anthropic/v12"
    on_disk: list[bool] = []

    async def sleep(seconds: float) -> None:
        assert seconds == rec.BATCH_POLL_S
        retrieves = len(list(raw.glob("retrieve-*.response.json")))
        on_disk.append((raw / "create.request.json").is_file() and retrieves == len(on_disk) + 1)

    code = await rec.main_async(
        ["--spike", "v12", "--live", "--max-wait-hours", "2"],
        root=tmp_path / "raw",
        fixtures_out=tmp_path / "fixtures",
        environ={"JOBHUNTER_ANTHROPIC_API_KEY": FAKE_KEY},
        transport_factory=lambda: fake.RecordedSSETransport(answers),
        sleep=sleep,
        now=clock,
    )
    assert code == rec.STOPPED
    (entry,) = json.loads((tmp_path / "raw/anthropic-budget.json").read_text("utf-8"))["entries"]
    assert (entry["state"], entry["settled_usd"]) == ("created", None)
    assert on_disk and all(on_disk)
    assert not (tmp_path / "fixtures").exists()  # nothing committed before the results


@pytest.mark.parametrize(
    "argv",
    [
        ["--spike", "v5", "--dry-run", "--max-wait-hours", "2"],
        ["--spike", "v12", "--dry-run", "--max-wait-hours", "0"],
        ["--spike", "v12", "--dry-run", "--max-wait-hours", "25"],
    ],
)
def test_max_wait_hours_is_for_the_batch_spike_within_a_day(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code = rec.main(argv, root=tmp_path / "raw", fixtures_out=tmp_path / "fixtures", environ={})
    assert code == rec.REFUSED and "--max-wait-hours is for v12" in capsys.readouterr().out


async def test_a_create_without_a_known_outcome_is_never_sent_again(tmp_path: Path) -> None:
    sending = {
        "spike": "v12",
        "case": "batch#1",
        "logical_case": "batch",
        "attempt": 1,
        "state": "sending",
        "sent_at": "2026-10-29T12:00:00+00:00",
        "worst_case_usd": "0.009565",
        "settled_usd": None,
    }
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/anthropic-budget.json").write_text(json.dumps({"entries": [sending]}), "utf-8")
    code = await rec.main_async(
        ["--spike", "v12", "--live"],
        root=tmp_path / "raw",
        fixtures_out=tmp_path / "fixtures",
        environ={"JOBHUNTER_ANTHROPIC_API_KEY": FAKE_KEY},
        transport_factory=lambda: (_ for _ in ()).throw(AssertionError("nothing is sent")),
        sleep=_no_sleep(),
    )
    assert code == rec.STOPPED
    assert json.loads((tmp_path / "raw/anthropic-budget.json").read_text("utf-8"))["entries"] == [sending]


async def test_commit_recorded_rebuilds_the_documents_from_the_raw_capture_without_thinking(
    tmp_path: Path,
) -> None:
    code, directory = await fake_run(tmp_path)
    assert code == rec.OK
    # the API's answer carries a thinking block before the text (the fake's never does)
    raw = tmp_path / "raw/anthropic/v12/results.response.jsonl"
    lines = [json.loads(line) for line in raw.read_text("utf-8").splitlines()]
    succeeded = next(line for line in lines if line["result"]["type"] == "succeeded")
    thinking = {"type": "thinking", "thinking": "Synthetic reasoning.", "signature": "U3ludGhldGlj" * 8}
    succeeded["result"]["message"]["content"].insert(0, thinking)
    raw.write_text("".join(json.dumps(line) + "\n" for line in lines), "utf-8")
    again = await rec.main_async(
        ["--spike", "v12", "--commit-recorded"],
        root=tmp_path / "raw",
        fixtures_out=tmp_path / "fixtures",
        environ={},
        transport_factory=lambda: (_ for _ in ()).throw(AssertionError("nothing is sent")),
    )
    assert again == rec.OK
    committed = json.loads((directory / "results.json").read_text("utf-8"))
    mine = next(line for line in committed if line["custom_id"] == succeeded["custom_id"])
    assert mine["result"]["message"]["content"][0] == {
        "type": "thinking",
        "thinking": fake.THINKING_OMITTED,
        "signature": fake.SIGNATURE_OMITTED,
    }
    text = (directory / "results.json").read_text("utf-8")
    assert "Synthetic reasoning." not in text and "U3ludGhldGlj" not in text


async def test_commit_recorded_needs_a_settled_batch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await rec.main_async(
        ["--spike", "v12", "--commit-recorded"],
        root=tmp_path / "raw",
        fixtures_out=tmp_path / "fixtures",
        environ={},
    )
    assert code == rec.REFUSED and "no settled batch" in capsys.readouterr().out
    assert not (tmp_path / "fixtures").exists()


def _raw_create(tmp: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((tmp / "raw/anthropic/v12/create.request.json").read_text("utf-8"))[
        "body"
    ]
    return raw


def test_the_v12_dry_run_builds_the_batch_offline_within_the_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = rec.main(
        ["--spike", "v12", "--dry-run"],
        root=tmp_path / "raw",
        fixtures_out=tmp_path / "fixtures",
        environ={},
        transport_factory=lambda: (_ for _ in ()).throw(AssertionError("a dry run opens no transport")),
    )
    out = capsys.readouterr().out
    assert code == rec.OK, out
    assert out.count("payload ok") == 4 and "BAD" not in out  # three items and the create
    assert "committed_usd=" in out and not (tmp_path / "raw").exists()


# --- the replay through the item decision ---------------------------------------------------------------


_INSERT_POLICY: Final = sa.text(
    "INSERT INTO model_policies (policy_hash, payload) VALUES (:hash, CAST(:payload AS jsonb)) "
    "ON CONFLICT (policy_hash) DO NOTHING"
)
_ACTIVATE_POLICY: Final = sa.text(
    "UPDATE active_config SET model_policy_id = (SELECT id FROM model_policies WHERE policy_hash = :hash) "
    "WHERE id = 1"
)


async def haiku_generator_policy(engine: AsyncEngine) -> None:
    """The seeded model policy with its generator on Haiku, made the active one (the reset restores it)."""
    payload: dict[str, Any] = copy.deepcopy(get_object(load_settings_v1(), "scoring.model_policy"))
    payload["nodes"]["generate"]["model"] = HAIKU
    digest = hashlib.sha256(canonical_json(payload)).digest()
    async with engine.begin() as conn:
        await conn.execute(_INSERT_POLICY, {"hash": digest, "payload": json.dumps(payload)})
        await conn.execute(_ACTIVATE_POLICY, {"hash": digest})


@pytest.mark.integration
async def test_the_recorded_results_replay_through_the_item_decision(
    documents: Documents,
    engine: AsyncEngine,
    op_runner: Any,
    fake_llm: Any,
    queue_app: Any,
    clean_db: None,
) -> None:
    await haiku_generator_policy(engine)
    # a submission needs `batch_min_targets` (200) targets: the recorded lines answer the first three items
    submitted = await submit_batch(engine, op_runner, fake_llm, BATCH_MIN_TARGETS)
    replaying = submitted.custom_ids[:3]
    ours = dict(zip(sorted(line["custom_id"] for line in documents.results), replaying, strict=True))
    ids = await item_ids(engine)
    attempts = dict(await items_of(engine, submitted.submission_id))
    targets = {submitted.custom_id(t): t.target_id for t in submitted.setup.targets}
    submission = batch_items.Submission(
        submitted.submission_id, "submitted", submitted.setup.bulk_operation_id
    )
    replayed: dict[str, str] = {}
    for line in documents.results:
        mine = ours[line["custom_id"]]
        parsed = MessageBatchIndividualResponse.model_validate({**line, "custom_id": mine})
        result = llm_batches._item(parsed)
        target = targets[mine]
        item = batch_items.Item(ids[mine], target, attempts[target])
        async with engine.begin() as conn:
            decision = await batch_items.process_item(
                conn, submission=submission, item=item, result=result, now=utcnow()
            )
        assert decision.result_type == result.result_type
        replayed[mine] = result.result_type
    entries = await entries_by_custom_id(engine)
    for line in documents.results:
        mine, result = ours[line["custom_id"]], line["result"]
        entry = entries[mine]
        if result["type"] == "succeeded":
            u = result["message"]["usage"]
            usage = pricing.Usage(
                u["input_tokens"],
                u["output_tokens"],
                u["cache_creation_input_tokens"] or 0,
                u["cache_read_input_tokens"] or 0,
            )
            assert (entry.state, entry.status) == ("billed", "completed")
            assert entry.settled_usd == pricing.settled_usd(HAIKU, usage, batch=True)
        else:
            assert (entry.state, entry.evidence, entry.settled_usd) == (
                "not_billed",
                _EVIDENCE[result["type"]],
                0,
            )
            assert entry.status == "error"
            if (
                result["type"] == "errored"
            ):  # Carry T8-b: the SDK's nested error.error.type, never the message
                kind = result["error"]["error"]["type"]
                assert entry.error_code.endswith(f":{kind}")
    assert sorted(replayed) == sorted(replaying)
