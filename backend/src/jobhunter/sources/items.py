"""Raw items (spec §7.7, §7.16.1, §9.2 node 5): one `raw_items` row per element of a stored response.

`parse_response` parses one stored response of a run in the caller's transaction. Its first statement holds
the run row `FOR SHARE` with the owner's predicate (`ledger.lock_run_owner`: `FencedOut` otherwise, and
nothing is written); then the response row `FOR UPDATE`, so a second parse of the same response waits for the
first and finds it done. A response with `parsed_at` set returns its stored counts and writes nothing.
Otherwise the adapter reads the stored body (`receipts.load_response`): `elements` gives the canonical bytes
of each element of its item array (ruling T15-a), and `parse` exactly one outcome per element, the outcome of
element `i` at index `i`; any other answer is an `InvariantViolation`, and nothing is written. Each element
becomes one row:

- `Parsed` is `pending`, with the `NormalizedPosting` as `normalized` (`model_dump(mode="json")`);
- `OutOfScope(reason)` is `out_of_scope(reason)` (`not_germany`, or SmartRecruiters' `detail_call`, T15-c);
- `DuplicateInPage(n)` is `duplicate_in_page('first_index=<n>')`;
- `Rejected(code)` is `rejected(code)`.

Every row has the outcome's `native_id` and `item_ts` and `item_sha256 = element_sha256(element)`; a disposed
one has its `disposed_at`. A `SourceError` from `parse` (the whole array unreadable) rejects every element
`item_contract`; one from `elements`, on a body whose envelope passed `check_envelope` when it was stored, is
a bug and propagates (ruling T15-b). The rows are inserted `ON CONFLICT (raw_response_id, item_index) DO
NOTHING`, then the response gets `parsed_at = now()` and `item_count = len(elements)`, which must be the count
it was stored with (the envelope's, `peek_native_ids`): the I1 sum of §17.2 is taken over that count.

The stored counts are the parse's dispositions read back from the rows: an item that left `pending` since then
(`linked`, `deferred`, `out_of_scope('source_purged')` or `rejected('identity_error')`, §7.16.1) counts as
pending.

`pending_items_of_run` lists the `pending` items of a run's responses, in id order (node 6 resolves them).
`replay_rejected` is the explicit replay `rejected -> pending` of §7.16.1 (M4 exposes it): every item whose
stored body is not purged and was fetched at most `RAW_BODY_TTL` (30 days) ago is re-parsed from that body
by the adapter of its source instance. An item the new parse gives as `Parsed` moves to `pending` with its
`normalized`, `native_id` and `item_ts`; every other item stays `rejected`. It returns the number of items
moved.
"""

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal, assert_never

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition
from jobhunter.errors import InvariantViolation
from jobhunter.observability.logging import get_logger
from jobhunter.quota import ledger
from jobhunter.sources.base import (
    ITEM_CONTRACT,
    DuplicateInPage,
    ItemOutcome,
    OutOfScope,
    Parsed,
    RawPage,
    Rejected,
    RequestSpec,
    SourceAdapter,
    SourceError,
    element_sha256,
)
from jobhunter.sources.instances import InstanceRow, list_instances
from jobhunter.sources.receipts import RAW_BODY_TTL, load_response

if TYPE_CHECKING:
    # `worker.runs` imports `observability.health`, which imports the quota ledger: the owner type only.
    from jobhunter.worker.runs import RunOwner

Disposition = Literal["pending", "out_of_scope", "duplicate_in_page", "rejected"]
# The reasons with which a `pending` item leaves for a disposition a parse also gives (§7.16.1).
SOURCE_PURGED: Final = "source_purged"
IDENTITY_ERROR: Final = "identity_error"

_RESPONSE: Final = q(
    "SELECT r.parsed_at, r.item_count, f.spec FROM raw_responses r "
    "JOIN fetch_requests f ON f.id = r.fetch_request_id WHERE r.id = :id AND f.run_id = :run FOR UPDATE OF r"
)
_STORED: Final = q(
    "SELECT count(*) FILTER (WHERE disposition IN ('pending', 'linked', 'deferred') "
    "OR (disposition = 'out_of_scope' AND reason_code = :purged) "
    "OR (disposition = 'rejected' AND reason_code = :identity)) AS pending, "
    "count(*) FILTER (WHERE disposition = 'out_of_scope' AND reason_code <> :purged) AS out_of_scope, "
    "count(*) FILTER (WHERE disposition = 'duplicate_in_page') AS duplicate_in_page, "
    "count(*) FILTER (WHERE disposition = 'rejected' AND reason_code <> :identity) AS rejected "
    "FROM raw_items WHERE raw_response_id = :id"
)
_INSERT_PENDING: Final = q(
    "INSERT INTO raw_items (raw_response_id, item_index, native_id, item_ts, item_sha256, disposition, "
    "normalized) VALUES (:rr, :index, :native_id, :item_ts, :sha, 'pending', :normalized) "
    "ON CONFLICT (raw_response_id, item_index) DO NOTHING",
    jsonb=["normalized"],
)
_INSERT_DISPOSED: Final = q(
    "INSERT INTO raw_items (raw_response_id, item_index, native_id, item_ts, item_sha256, disposition, "
    "reason_code, disposed_at) "
    "VALUES (:rr, :index, :native_id, :item_ts, :sha, :disposition, :reason, now()) "
    "ON CONFLICT (raw_response_id, item_index) DO NOTHING"
)
_MARK_PARSED: Final = q(
    "UPDATE raw_responses SET parsed_at = now(), item_count = :n WHERE id = :id AND parsed_at IS NULL"
)
_PENDING_OF_RUN: Final = q(
    "SELECT i.id FROM raw_items i JOIN raw_responses r ON r.id = i.raw_response_id "
    "JOIN fetch_requests f ON f.id = r.fetch_request_id WHERE f.run_id = :run AND i.disposition = 'pending' "
    "ORDER BY i.id"
)
_REJECTED: Final = q(
    "SELECT i.id, i.raw_response_id, i.item_index, r.fetched_at, r.purged_at, f.spec, f.source_instance_id "
    "FROM raw_items i JOIN raw_responses r ON r.id = i.raw_response_id "
    "JOIN fetch_requests f ON f.id = r.fetch_request_id "
    "WHERE i.id = ANY(CAST(:ids AS bigint[])) AND i.disposition = 'rejected' ORDER BY i.id FOR UPDATE OF i"
)

log = get_logger("jobhunter.sources.items")


@dataclass(frozen=True)
class ParseCounts:
    """The dispositions a parse gave the elements of one response (§7.16.1)."""

    pending: int
    out_of_scope: int
    duplicate_in_page: int
    rejected: int

    @property
    def total(self) -> int:
        """Every element of the response: its `item_count`."""
        return self.pending + self.out_of_scope + self.duplicate_in_page + self.rejected


def _one_per_element(outcomes: Sequence[ItemOutcome], elements: int, raw_response_id: int) -> None:
    """`InvariantViolation` unless `outcomes` is one outcome per element, element `i`'s at index `i`
    (§5.1)."""
    if [outcome.item_index for outcome in outcomes] != list(range(elements)):
        raise InvariantViolation(
            f"raw response {raw_response_id}: the parse is not one outcome per element in item_index order "
            f"({len(outcomes)} outcomes for {elements} elements)"
        )


def _outcomes(
    adapter: SourceAdapter, page: RawPage, raw_response_id: int, elements: int
) -> list[ItemOutcome]:
    """The adapter's outcome of each element; every element `rejected(item_contract)` when `parse` finds the
    item array unreadable as a whole (a `SourceError`)."""
    try:
        outcomes = adapter.parse(page)
    except SourceError as err:  # the kind and the code only: a message may carry content
        log.warning(
            "item_array_rejected", raw_response_id=raw_response_id, error_kind=err.kind, error_code=err.code
        )
        detail = f"the item array is unreadable ({err.code})"
        return [Rejected(index, None, None, ITEM_CONTRACT, detail) for index in range(elements)]
    _one_per_element(outcomes, elements, raw_response_id)
    return outcomes


def _row(raw_response_id: int, outcome: ItemOutcome, element: bytes) -> tuple[Disposition, dict[str, Any]]:
    """The disposition of an element's outcome and the parameters of its row."""
    row: dict[str, Any] = {
        "rr": raw_response_id,
        "index": outcome.item_index,
        "native_id": outcome.native_id,
        "item_ts": outcome.item_ts,
        "sha": element_sha256(element),
    }
    match outcome:
        case Parsed():
            return "pending", {**row, "normalized": outcome.normalized.model_dump(mode="json")}
        case OutOfScope():
            return "out_of_scope", {**row, "disposition": "out_of_scope", "reason": outcome.reason}
        case DuplicateInPage():
            reason = f"first_index={outcome.first_index}"
            return "duplicate_in_page", {**row, "disposition": "duplicate_in_page", "reason": reason}
        case Rejected():
            return "rejected", {**row, "disposition": "rejected", "reason": outcome.reason_code}
        case _:
            assert_never(outcome)


async def _stored_counts(conn: AsyncConnection, raw_response_id: int) -> ParseCounts:
    params = {"id": raw_response_id, "purged": SOURCE_PURGED, "identity": IDENTITY_ERROR}
    row = (await conn.execute(_STORED, params)).one()
    return ParseCounts(int(row.pending), int(row.out_of_scope), int(row.duplicate_in_page), int(row.rejected))


async def parse_response(
    conn: AsyncConnection, raw_response_id: int, *, adapter: SourceAdapter, owner: "RunOwner"
) -> ParseCounts:
    """Parse the stored response `raw_response_id` of `owner`'s run into its `raw_items` rows, in the caller's
    transaction (see the module docstring); its counts. Raises `FencedOut` for a superseded owner and
    `LookupError` for a response that is not one of the run's, having written nothing."""
    await ledger.lock_run_owner(conn, owner)
    response = (await conn.execute(_RESPONSE, {"id": raw_response_id, "run": owner.run_id})).first()
    if response is None:
        raise LookupError(f"raw response {raw_response_id} is not a response of run {owner.run_id}")
    if response.parsed_at is not None:
        return await _stored_counts(conn, raw_response_id)
    page = await load_response(conn, raw_response_id, RequestSpec.from_json(response.spec))
    elements = adapter.elements(page)
    if response.item_count is not None and response.item_count != len(elements):
        raise InvariantViolation(
            f"raw response {raw_response_id} was stored with {response.item_count} items, "
            f"its body has {len(elements)} elements"
        )
    outcomes = _outcomes(adapter, page, raw_response_id, len(elements))
    tally: dict[Disposition, int] = {"pending": 0, "out_of_scope": 0, "duplicate_in_page": 0, "rejected": 0}
    pending: list[dict[str, Any]] = []
    disposed: list[dict[str, Any]] = []
    for outcome, element in zip(outcomes, elements, strict=True):
        disposition, row = _row(raw_response_id, outcome, element)
        tally[disposition] += 1
        (pending if disposition == "pending" else disposed).append(row)
    if pending:
        await conn.execute(_INSERT_PENDING, pending)
    if disposed:
        await conn.execute(_INSERT_DISPOSED, disposed)
    await conn.execute(_MARK_PARSED, {"id": raw_response_id, "n": len(elements)})
    counts = ParseCounts(**tally)
    log.info("response_parsed", raw_response_id=raw_response_id, items=counts.total, **asdict(counts))
    return counts


async def pending_items_of_run(conn: AsyncConnection, run_id: int) -> list[int]:
    """The ids of the `pending` raw items of the run's responses, in id order."""
    return [
        int(raw_item_id) for raw_item_id in (await conn.execute(_PENDING_OF_RUN, {"run": run_id})).scalars()
    ]


async def replay_rejected(
    conn: AsyncConnection,
    *,
    raw_item_ids: Sequence[int],
    now: datetime,
    adapters: Callable[[InstanceRow], SourceAdapter],
) -> int:
    """The replay `rejected -> pending` of the `rejected` items among `raw_item_ids` (see the module
    docstring), in the caller's transaction, each item locked `FOR UPDATE`; the number of items moved. An id
    that is not a `rejected` item is skipped."""
    rows = (await conn.execute(_REJECTED, {"ids": list(raw_item_ids)})).all()
    if not rows:
        return 0
    instances = {instance.id: instance for instance in await list_instances(conn)}
    by_response: dict[int, list[Any]] = {}
    for row in rows:
        by_response.setdefault(int(row.raw_response_id), []).append(row)
    moved = 0
    for raw_response_id, items in by_response.items():
        first = items[0]
        if first.purged_at is not None or now - first.fetched_at > RAW_BODY_TTL:  # payload <= 30 d
            continue
        try:
            page = await load_response(conn, raw_response_id, RequestSpec.from_json(first.spec))
        except LookupError:  # the identical body it references is purged
            continue
        adapter = adapters(instances[int(first.source_instance_id)])
        try:
            outcomes = adapter.parse(page)
        except SourceError:  # the item array is still unreadable: its items stay rejected
            continue
        _one_per_element(outcomes, len(adapter.elements(page)), raw_response_id)
        for item in items:
            outcome = outcomes[int(item.item_index)]
            if not isinstance(outcome, Parsed):
                continue
            fields = {
                "normalized": outcome.normalized.model_dump(mode="json"),
                "native_id": outcome.native_id,
                "item_ts": outcome.item_ts,
            }
            await transition(
                conn, "raw_items", int(item.id), ["rejected"], "pending", reason=CLEAR_REASON, set_=fields
            )
            moved += 1
    log.info("rejected_items_replayed", asked=len(raw_item_ids), rejected=len(rows), moved=moved)
    return moved
