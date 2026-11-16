"""The two-year-old seed of the deletion-graph tests (plan M5 Task 13; Tasks 14-16 and 25 build on it).

`seed_two_year_old_graph(conn, *, now, live_submission="submitted") -> SeedIds` writes, in the caller's
transaction, rows in every table of the `public` schema (`migrate`'s configuration rows, which every test
database holds, are reused: the sources, the transition matrix, the seeded policies and settings version 1,
the schedule, dead-man and ranking singletons), each dated about `SEED_AGE` (730 days) before `now`. It
follows the seed rule of the plan's Global Constraints (Fixtures): every row is inserted directly in its state
on the real schema with every CHECK, unique and partial-unique index and trigger enabled; a pair of rows that
name each other gets the second key by an `UPDATE` of a column no trigger watches, or by `transition()` along
the matrix (a fetch request's `dispatched → succeeded`); the singletons hold one row at most (one live batch
submission, `batch_one_in_flight`; one `active` bulk operation; no open profile draft, no running export,
collect run or score cycle, no current quota window); distinct keys stay distinct. The caller commits: the
deferred foreign keys are checked then. `users` and `worker_heartbeat` get their row only when they have none
(a test's `make_user` or `current_epoch` may have made it).

What it holds (every text invented, ids and hashes stand-ins):
- configuration: a scope version and settings version 2 over it, the two profile versions (each with its CV
  file, its confirmed draft, its extraction call and its `summary` vector; the newer one active, with a
  `title:0` vector too), a discarded draft, a tuple per version (the newer one active), a ready query
  proposal with its call and the query it proposed beside a user's query;
- the purgeable version (Task 14), the oldest of the three, confirmed first: neither active nor the previous
  one, no feedback names it, its one case is inactive and its tuple's targets ended terminally two years
  ago, so a profile purge at `now` takes it. Its CV file (a name of the store's form, with its texts), its
  confirmed draft with its content and its extraction call (`input_refs` naming the draft), its `summary`
  vector, its tuple with a published target on `closed_job`'s first revision and a cancelled one on the
  active job, an inactive golden case labelled from that result (with its eval result and call), and its
  query proposals: a `ready` one (its call, and an earlier attempt's call that no proposal names), a
  `failed` one (its call, named by no proposal) and a `running` one with its `pending` `propose_queries`
  operation and that operation's call still `dispatched` (its entry `dispatched`), each call's `input_refs`
  naming the proposal and the version;
- collection: a watchlist board and its Greenhouse instance, a collect run with its attempt, a coverage cell
  and interval, an Adzuna quota window (closed) with its billed ledger entry, the run's fetch request with
  its response and two raw items (one linked to an observation), a complete ATS snapshot with a member and
  an absence;
- jobs: `closed_job`, the full graph a tombstone deletes (two revisions, an Adzuna and an ATS posting with
  observations, three keys (one, `htidocid`, also supplied by the active job's posting and the other job's),
  lineage, occurrences, hard-filter results, user actions with a note, the representative of a group of three,
  every target state but the batch and claim ones over its revisions and those of the two jobs merged into it
  (a chain, `merged_job_ids`), an inactive golden case with its eval result and call, feedback, merge events
  (a reversed pair among them), identity conflicts that name it (an open one's `dedup.identity_conflict`
  alert), a decision (and another of the `filtered` state job's), an open `dedup.ambiguous_grouping` alert);
  closed or stale state jobs holding an `in_progress` target (online entry `dispatched`), the `in_batch`
  targets, a `filtered_out` one, and the targets of the `reconciled` and `released` submissions; the active
  job (its `pending` target the `abandoned` submission's), the other job (restored from an older tombstone,
  with that tombstone's key), the two other members of the group (one with a `deferred_duplicate` target, one
  `budget_deferred`); the exempt jobs (`exempt_job_ids`: saved, applied, and one an active golden case
  labels);
- scoring: a completed cycle, a completed and the one active bulk operation, the lane credits, the one live
  submission in state `live_submission` holding every `in_batch` target (its entries `reserved` while
  `preparing`, else `dispatched`) and the terminal ones: `reconciled`, `abandoned`, and `released` with
  `released_at` (its CHECK allows that only in `released`, `ended` or `reconciled`); results with their
  evidence, current and superseded instances, drafts, critic notes; the budget month's counters equal to its
  entries' (a purge's release subtracts from them);
- the rest: a session, a login attempt and lockout, an idempotency row, alerts open and resolved, operations,
  a shipped deletion manifest with its shipment, released fence and purged batch item (its `scope` a content
  of the format and its `file_sha256` that content's file hash, as A3 records them: `migrate` rewrites the
  missing file of a recorded manifest from its scope, M5 Task 23), a backup set and its restore test, an
  expired and a ready export;
- for the source purge (Task 15): every observation holds a whole normalized posting
  (`tests.support.observations.normalized`), so a job can be rebuilt from its remaining postings.
  `closed_job` is the mixed job (an Adzuna and an ATS posting; its current revision's lineage names both);
  the Adzuna-only jobs (`adzuna_only_job_ids`) are the active job, the other job (named by merge events) and
  an active job into which another was merged (the merge event names both; both revisions have Adzuna
  lineage); each golden case draws on Adzuna (`det_input_sources`). Both new jobs are active, so no tombstone
  is due for them;
- for delete-everything (Task 16): a second online try in flight on an active job (its call's entry
  `reserved`) beside the claimed target's `dispatched` one; a `running` eval run with a call in flight
  (`dispatched`, naming no case and no revision, so no other purge takes it); a scheduled run that ended
  `succeeded` and was never pinged (the dead-man's watermark is NULL); an open warn alert never pushed
  (`operation.failed`) and an open, unacknowledged `llm.reservation_bound_violated`, which pauses the score
  lane, beside the resolved `retention.failed`.

A scenario that needs another live submission state runs on another seeded database (`live_submission`).
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import uuid4

import zstandard
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.dedup.identity import tombstone_key_hash
from jobhunter.normalize.keys import AdKey
from jobhunter.privacy.manifests import ManifestContent, canonical_bytes
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.support.m2 import MINIMAL_PROFILE
from tests.support.observations import normalized
from tests.support.scoring import REASON_OF

InFlight = Literal["preparing", "submit_started", "submit_ambiguous", "submitted", "ended"]
SEED_AGE: Final = timedelta(days=730)
# The key of `closed_job`'s Adzuna posting, and the shared `htidocid` key its posting supplies first.
CLOSED_ADZUNA_KEY: Final = AdKey("adzuna", "seed-adz-1")
SHARED_KEY: Final = AdKey("htidocid", "seed-shared")

_UNIT_VECTOR: Final = "[" + ",".join(["0"] * 1023 + ["1"]) + "]"
_MODEL: Final = "claude-sonnet-5-5"
_SCORING: Final = ("generate", "revise", "critic")
_ACTIVE: Final = q(
    "SELECT settings_version_id, scope_version_id, model_policy_id, execution_config_id, "
    "evaluation_policy_id, rubric_version_id, prompt_schema_version_id FROM active_config WHERE id = 1"
)
_NEXT_ID: Final = "SELECT nextval(pg_get_serial_sequence(:table, 'id'))"


@dataclass(frozen=True)
class SeedIds:
    """The seed's rows Task 13's tests read (later tasks extend it)."""

    live_submission_id: int
    closed_job_id: int  # the full-graph root
    due_job_ids: tuple[int, ...]  # every root a tombstoning pass at `now` opens, `closed_job_id` among them
    merged_job_ids: tuple[int, ...]  # merged into `closed_job_id`, a chain, deepest first
    # closed or stale, but saved, applied or labelled by an active golden case
    exempt_job_ids: tuple[int, ...]
    # active; its posting supplies `SHARED_KEY` first after `closed_job_id`'s (the key's heir)
    active_job_id: int
    lone_conflict_id: int  # open, on the active job's posting, its one candidate `closed_job_id`
    shared_conflict_id: int  # open, its candidates `closed_job_id` and `active_job_id`
    # Task 14: the user's three versions, the oldest one purgeable at `now` (module docstring)
    active_version_id: int = 0
    previous_version_id: int = 0  # the newest confirmed before the active one
    purgeable_version_id: int = 0
    purgeable_tuple_id: int = 0
    purgeable_draft_id: int = 0
    purgeable_cv_file_id: int = 0
    purgeable_case_id: int = 0  # inactive, on `closed_job`'s first revision
    ready_proposal_id: int = 0
    failed_proposal_id: int = 0
    running_proposal_id: int = 0
    running_operation_id: int = 0  # the running proposal's `pending` `propose_queries` operation
    in_flight_call_id: int = 0  # that operation's call, `dispatched`
    # every `propose_queries` and `extract_profile` call of the purgeable version, in id order
    purgeable_call_ids: tuple[int, ...] = ()
    ready_export_id: int = 0
    # Task 15: the source purge's jobs (module docstring)
    adzuna_only_job_ids: tuple[int, ...] = ()  # every job whose only postings are Adzuna's
    adzuna_merge_root_id: int = 0  # Adzuna-only, `adzuna_merged_job_id` merged into it
    adzuna_merged_job_id: int = 0
    golden_job_id: int = 0  # the active golden case's job (the case draws on Adzuna)
    batched_job_ids: tuple[int, ...] = ()  # the jobs of the live submission's `in_batch` targets
    # Task 16: what delete-everything settles, pings and keeps (module docstring)
    reserved_entry_id: int = 0  # an online scoring try's entry, `reserved`
    dispatched_entry_id: int = 0  # the claimed target's online scoring try's entry, `dispatched`
    eval_entry_id: int = 0  # a running eval run's call's entry, `dispatched`
    running_eval_run_id: int = 0
    unpinged_run_id: int = 0  # a scheduled run that ended `succeeded`, never pinged (the watermark NULL)
    open_alert_id: int = 0  # warn, never pushed
    bound_violation_alert_id: int = 0  # `llm.reservation_bound_violated`, open and unacknowledged
    resolved_alert_id: int = 0  # resolved two years ago
    # Task 25: the inactive golden case on `closed_job`'s current revision (the old version's), which a
    # golden-case deletion takes while no running eval run includes it
    closed_case_id: int = 0


@dataclass(frozen=True)
class _Job:
    id: int
    revisions: tuple[int, ...]  # by revision number; the last is current

    @property
    def current(self) -> int:
        return self.revisions[-1]


@dataclass(frozen=True)
class _Active:
    settings_version_id: int
    scope_version_id: int
    model_policy_id: int
    execution_config_id: int
    evaluation_policy_id: int
    rubric_version_id: int
    prompt_schema_version_id: int


@dataclass
class _Seeder:
    conn: AsyncConnection
    now: datetime
    live: InFlight
    n: int = 0
    counted: dict[str, Decimal] = field(
        default_factory=lambda: {"scoring": Decimal(0), "nonscoring": Decimal(0)}
    )

    @property
    def t0(self) -> datetime:
        return self.now - SEED_AGE

    def at(self, minutes: int = 0, *, days: int = 0) -> datetime:
        return self.t0 + timedelta(days=days, minutes=minutes)

    def uniq(self, label: str) -> bytes:
        self.n += 1
        return f"seed-{label}-{self.n}".encode()

    @property
    def month(self) -> date:
        return date(self.t0.year, self.t0.month, 1)

    async def row(
        self,
        table: str,
        values: Mapping[str, object],
        *,
        casts: Mapping[str, str] | None = None,
        overriding: bool = False,
        returning: bool = True,
    ) -> int:
        """Insert one row; a dict value (or a column cast to `jsonb`) is bound as JSONB, another cast is
        written `CAST(:col AS <type>)`. Its `id` unless `returning` is false (then 0)."""
        casts = dict(casts or {})
        jsonb = [
            name for name, value in values.items() if isinstance(value, dict) or casts.get(name) == "jsonb"
        ]
        binds = [
            f"CAST(:{name} AS {casts[name]})" if name in casts and casts[name] != "jsonb" else f":{name}"
            for name in values
        ]
        override = " OVERRIDING SYSTEM VALUE" if overriding else ""
        tail = " RETURNING id" if returning else ""
        statement = f"INSERT INTO {table} ({', '.join(values)}){override} VALUES ({', '.join(binds)}){tail}"  # noqa: S608
        result = await self.conn.execute(q(statement, jsonb=jsonb), dict(values))
        return int(result.scalar_one()) if returning else 0

    async def next_id(self, table: str) -> int:
        return int((await self.conn.execute(q(_NEXT_ID), {"table": table})).scalar_one())

    async def update(self, statement: str, **params: object) -> None:
        await self.conn.execute(q(statement), params)


# ---- the parts ---------------------------------------------------------------------------------------------


async def _active(s: _Seeder) -> _Active:
    row = (await s.conn.execute(_ACTIVE)).one()
    return _Active(*(int(value) for value in row))


async def _ops_rows(s: _Seeder) -> None:
    """The user, the worker heartbeat (when absent), a session, a login attempt and lockout, an idempotency
    row."""
    await s.update(
        "INSERT INTO users (id, username, password_hash, password_changed_at, created_at) "
        "VALUES (1, 'seed-user', '$argon2id$seed', :t, :t) ON CONFLICT DO NOTHING",
        t=s.at(),
    )
    await s.update(
        "INSERT INTO worker_heartbeat (id, worker_instance_id, worker_epoch, hostname, pid, "
        "lock_backend_pid, app_version, started_at, heartbeat_at) "
        "VALUES (1, gen_random_uuid(), 1, 'seed', 1, 1, 'seed', :t, :t) ON CONFLICT DO NOTHING",
        t=s.at(),
    )
    await s.row(
        "sessions",
        {
            "token_hmac": s.uniq("session"),
            "user_id": 1,
            "created_at": s.at(),
            "last_seen_at": s.at(5),
            "idle_expires_at": s.at(days=1),
            "absolute_expires_at": s.at(days=7),
            "ip": "198.51.100.7",
            "user_agent_sha256": s.uniq("ua"),
        },
        casts={"ip": "inet"},
        returning=False,
    )
    await s.row(
        "login_attempts",
        {"at": s.at(), "ip": "198.51.100.7", "username_hmac": s.uniq("user"), "success": True},
        casts={"ip": "inet"},
    )
    await s.row(
        "login_lockouts",
        {"scope": "ip:198.51.100.8", "locked_until": s.at(15), "reason": "failures"},
        returning=False,
    )
    await s.row(
        "api_idempotency",
        {
            "route": "POST /api/seed",
            "idem_key": "seed-idempotency-0001",
            "request_sha256": s.uniq("request"),
            "status": "completed",
            "response_status": 202,
            "response_body": {"id": 1},
            "created_at": s.at(),
            "expires_at": s.at(days=7),
        },
    )


@dataclass(frozen=True)
class _Profile:
    old_version: int
    active_version: int
    old_tuple: int
    active_tuple: int


async def _entry(
    s: _Seeder,
    *,
    purpose: str,
    state: str,
    reserved: Decimal = Decimal("0.1"),
    batch: bool = False,
    bulk_operation_id: int | None = None,
    eval_run: bool = False,
    evidence: str | None = None,
    minutes: int = 0,
) -> int:
    """A request ledger entry of the seed's month, counted as the budget counts it (reserved while
    `reserved`, `dispatched` or `ambiguous`, settled once `billed` or `not_billed`)."""
    scoring = purpose in _SCORING and not eval_run
    settled = {"billed": reserved, "not_billed": Decimal(0)}.get(state)
    s.counted["scoring" if scoring else "nonscoring"] += reserved if settled is None else settled
    return await s.row(
        "llm_spend_ledger",
        {
            "budget_month": s.month,
            "kind": "request",
            "purpose": purpose,
            "eval_run": eval_run,
            "purpose_class": "scoring" if scoring else "nonscoring",
            "model_requested": _MODEL,
            "state": state,
            "reserved_usd": reserved,
            "settled_usd": settled,
            "input_tokens_bound": 1000,
            "batch": batch,
            "bulk_operation_id": bulk_operation_id,
            "admitted_cap_usd": Decimal(100),
            "counted_before_usd": Decimal(0),
            "evidence": evidence,
            "created_at": s.at(minutes),
            "dispatched_at": None if state == "reserved" or evidence == "never_dispatched" else s.at(minutes),
            "settled_at": s.at(minutes + 1) if state in ("billed", "not_billed", "ambiguous") else None,
        },
    )


_CALL_STATUS: Final[Mapping[str, str]] = {
    "reserved": "reserved",
    "dispatched": "dispatched",
    "billed": "completed",
    "not_billed": "cancelled",
    "ambiguous": "ambiguous",
}


async def _call(
    s: _Seeder,
    active: _Active,
    *,
    purpose: str,
    entry_state: str = "billed",
    transport: str = "stream",
    minutes: int = 0,
    bulk_operation_id: int | None = None,
    eval_run_id: int | None = None,
    evidence: str | None = None,
    **owners: object,
) -> int:
    """A call of `purpose` and its ledger entry in `entry_state` (the call's status follows it); `owners`
    are its attempt, target, batch item, draft, eval run or golden case columns."""
    ledger = await _entry(
        s,
        purpose=purpose,
        state=entry_state,
        batch=transport == "batch",
        bulk_operation_id=bulk_operation_id,
        eval_run=eval_run_id is not None,
        evidence=evidence,
        minutes=minutes,
    )
    status = _CALL_STATUS[entry_state]
    completed = status == "completed"
    logical = None if transport == "batch" else (2 if purpose == "critic" else 1)
    return await s.row(
        "llm_calls",
        {
            "purpose": purpose,
            "round": (1 if purpose == "critic" else 0) if purpose in _SCORING else None,
            "logical_call_no": logical if purpose in _SCORING else None,
            "eval_run_id": eval_run_id,
            "execution_config_id": active.execution_config_id,
            "model_requested": _MODEL,
            "model_served": _MODEL if completed else None,
            "effort": "medium",
            "max_tokens": 6000,
            "transport": transport,
            "prompt_template_version": "v2",
            "rendered_prompt_sha256": s.uniq("prompt"),
            "input_refs": {},
            "input_tokens_bound": 1000,
            "status": status,
            "error_code": "purged" if status == "ambiguous" else None,
            "stop_reason": "end_turn" if completed else None,
            "input_tokens": 900 if completed else None,
            "output_tokens": 400 if completed else None,
            "cost_usd": Decimal("0.05") if completed else None,
            "spend_ledger_id": ledger,
            "output_json": {} if completed and purpose in ("extract_profile", "propose_queries") else None,
            "started_at": s.at(minutes),
            "ended_at": s.at(minutes + 1) if status not in ("reserved", "dispatched") else None,
            **owners,
        },
    )


async def _profiles(s: _Seeder, active: _Active) -> _Profile:
    """Two confirmed profile versions with their CV files, drafts, extraction calls, vectors and tuples, the
    newer one active; a discarded draft; a ready proposal with its call and query, and a user's query."""
    versions: list[int] = []
    for number in (1, 2):
        cv = await s.row(
            "cv_files",
            {
                "sha256": s.uniq("cv"),
                "mime": "text/plain",
                "size_bytes": 64,
                "storage_path": f"{uuid4()}.bin",  # a name `CvStore.write` makes
                "raw_text_enc": b"\x00seed",
                "stripped_text": "Erfundener Lebenslauf.",
                "extraction_method": "pasted",
                "uploaded_at": s.at(number),
            },
        )
        version = await s.next_id("profile_versions")
        draft = await s.row(
            "profile_drafts",
            {
                "cv_file_id": cv,
                "status": "confirmed",
                "status_changed_at": s.at(number + 10),
                "reviewed_text": "Erfundener Lebenslauf.",
                "reviewed_text_sha256": s.uniq("reviewed"),
                "draft_json": dict(MINIMAL_PROFILE),
                "confirmed_profile_version_id": version,
                "confirm_response": {"profile_version_id": version},
                "created_at": s.at(number),
                "updated_at": s.at(number + 10),
            },
        )
        await s.row(
            "profile_versions",
            {
                "id": version,
                "version_no": await _next_version_no(s),
                "draft_id": draft,
                "cv_file_id": cv,
                "profile_json": dict(MINIMAL_PROFILE),
                "profile_hash": s.uniq("profile"),
                "confirmed_at": s.at(number + 10),
            },
            overriding=True,
            returning=False,
        )
        call = await _call(s, active, purpose="extract_profile", minutes=number + 5, profile_draft_id=draft)
        await s.update("UPDATE profile_drafts SET llm_call_id = :c WHERE id = :d", c=call, d=draft)
        versions.append(version)
    discarded_cv = await s.row(
        "cv_files", {"sha256": s.uniq("cv"), "mime": "text/plain", "size_bytes": 64, "uploaded_at": s.at(30)}
    )
    await s.row(
        "profile_drafts",
        {
            "cv_file_id": discarded_cv,
            "status": "discarded",
            "status_changed_at": s.at(31),
            "created_at": s.at(30),
            "updated_at": s.at(31),
        },
    )
    tuples = []
    for version in versions:
        tuples.append(
            await s.row(
                "llm_tuples",
                {
                    "profile_version_id": version,
                    "rubric_version_id": active.rubric_version_id,
                    "prompt_schema_version_id": active.prompt_schema_version_id,
                    "model_policy_id": active.model_policy_id,
                    "created_at": s.at(20),
                },
            )
        )
    await s.update(
        "UPDATE active_config SET profile_version_id = :v, llm_tuple_id = :t WHERE id = 1",
        v=versions[1],
        t=tuples[1],
    )
    for version, kinds in ((versions[0], ("summary",)), (versions[1], ("summary", "title:0"))):
        for kind in kinds:
            embedding = await _embedding(s, "query")
            await s.row(
                "profile_vectors",
                {"profile_version_id": version, "kind": kind, "embedding_id": embedding},
                returning=False,
            )
    call = await _call(s, active, purpose="propose_queries", minutes=40)
    proposal = await s.row(
        "query_proposals",
        {
            "profile_version_id": versions[0],
            "status": "ready",
            "items": [{"text": "Erfundene Suche", "lang": "de"}],
            "llm_call_id": call,
            "created_at": s.at(40),
        },
        casts={"items": "jsonb"},
    )
    await s.row(
        "queries",
        {
            "text": "erfundene suche",
            "lang": "de",
            "origin": "llm_proposal",
            "proposal_id": proposal,
            "created_at": s.at(41),
        },
    )
    return _Profile(versions[0], versions[1], tuples[0], tuples[1])


@dataclass(frozen=True)
class _Purgeable:
    version: int
    tuple_id: int
    draft: int
    cv_file: int
    ready: int
    failed: int
    running: int
    operation: int
    in_flight: int
    calls: tuple[int, ...]


async def _purgeable_profile(s: _Seeder, active: _Active) -> _Purgeable:
    """The oldest user version (module docstring), confirmed before the two others: its CV file, its confirmed
    draft with its extraction call, the version, its tuple, its `summary` vector and its three proposals with
    their calls and the running one's operation."""
    cv = await s.row(
        "cv_files",
        {
            "sha256": s.uniq("cv"),
            "mime": "text/plain",
            "size_bytes": 64,
            "storage_path": f"{uuid4()}.bin",
            "raw_text_enc": b"\x00seed",
            "stripped_text": "Erfundener alter Lebenslauf.",
            "extraction_method": "pasted",
            "uploaded_at": s.at(),
        },
    )
    version = await s.next_id("profile_versions")
    draft = await s.row(
        "profile_drafts",
        {
            "cv_file_id": cv,
            "status": "confirmed",
            "status_changed_at": s.at(10),
            "reviewed_text": "Erfundener alter Lebenslauf.",
            "reviewed_text_sha256": s.uniq("reviewed"),
            "draft_json": dict(MINIMAL_PROFILE),
            "confirmed_profile_version_id": version,
            "confirm_response": {"profile_version_id": version},
            "created_at": s.at(),
            "updated_at": s.at(10),
        },
    )
    await s.row(
        "profile_versions",
        {
            "id": version,
            "version_no": await _next_version_no(s),
            "draft_id": draft,
            "cv_file_id": cv,
            "profile_json": dict(MINIMAL_PROFILE),
            "profile_hash": s.uniq("profile"),
            "confirmed_at": s.at(10),
        },
        overriding=True,
        returning=False,
    )
    extract = await _call(
        s,
        active,
        purpose="extract_profile",
        minutes=5,
        profile_draft_id=draft,
        input_refs={"profile_draft_id": draft},
        output_json={"summary": "Erfundene Zusammenfassung."},
    )
    await s.update("UPDATE profile_drafts SET llm_call_id = :c WHERE id = :d", c=extract, d=draft)
    llm_tuple = await s.row(
        "llm_tuples",
        {
            "profile_version_id": version,
            "rubric_version_id": active.rubric_version_id,
            "prompt_schema_version_id": active.prompt_schema_version_id,
            "model_policy_id": active.model_policy_id,
            "created_at": s.at(11),
        },
    )
    embedding = await _embedding(s, "query")
    await s.row(
        "profile_vectors",
        {"profile_version_id": version, "kind": "summary", "embedding_id": embedding},
        returning=False,
    )

    def refs(proposal: int) -> dict[str, Any]:
        return {"proposal_id": proposal, "profile_version_id": version}

    proposed = {"queries": [{"text": "Erfundene alte Suche", "lang": "de"}]}
    ready_id, failed_id, running_id = [await s.next_id("query_proposals") for _ in range(3)]
    # the ready one: a first call whose output failed the check (named by no proposal), then the repair
    earlier = await _call(
        s, active, purpose="propose_queries", minutes=12, logical_call_no=1, input_refs=refs(ready_id)
    )
    repair = await _call(
        s,
        active,
        purpose="propose_queries",
        minutes=13,
        logical_call_no=2,
        input_refs=refs(ready_id),
        output_json=proposed,
    )
    failing = await _call(
        s, active, purpose="propose_queries", minutes=14, logical_call_no=1, input_refs=refs(failed_id)
    )
    for proposal, status, published in (
        (ready_id, "ready", {"items": proposed, "llm_call_id": repair}),
        (failed_id, "failed", {}),
        (running_id, "running", {}),
    ):
        await s.row(
            "query_proposals",
            {
                "id": proposal,
                "profile_version_id": version,
                "status": status,
                "created_at": s.at(12),
                **published,
            },
            overriding=True,
            returning=False,
        )
    operation = await _operation(
        s,
        "propose_queries",
        "pending",
        str(running_id),
        {"proposal_id": running_id, "scope_version_id": active.scope_version_id},
    )
    owner = {"kind": "operation", "id": operation, "claim_id": str(uuid4())}  # a claim since reclaimed
    in_flight = await _call(
        s,
        active,
        purpose="propose_queries",
        entry_state="dispatched",
        minutes=15,
        logical_call_no=1,
        input_refs={**refs(running_id), "owner": owner, "deadline_s": 300},
    )
    calls = (extract, earlier, repair, failing, in_flight)
    return _Purgeable(
        version, llm_tuple, draft, cv, ready_id, failed_id, running_id, operation, in_flight, calls
    )


async def _next_version_no(s: _Seeder) -> int:
    found = await s.conn.execute(q("SELECT coalesce(max(version_no), 0) + 1 FROM profile_versions"))
    return int(found.scalar_one())


async def _embedding(s: _Seeder, input_kind: str) -> int:
    return await s.row(
        "embeddings",
        {
            "embedding_model_id": "seed-model",
            "text_sha256": s.uniq("text"),
            "input_kind": input_kind,
            "vector": _UNIT_VECTOR,
            "created_at": s.at(),
        },
        casts={"vector": "halfvec"},
    )


async def _configuration(s: _Seeder, active: _Active) -> None:
    """A scope version and settings version 2 over it (the active configuration stays version 1's)."""
    scope = await s.row(
        "scope_versions", {"scope_hash": s.uniq("scope"), "payload": {}, "created_at": s.at()}
    )
    await s.update(
        "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, "
        "model_policy_id, execution_config_id, evaluation_policy_id, base_version_id, effect_classes, "
        "created_at) "
        "SELECT (SELECT max(version_no) + 1 FROM settings_versions), v.schema_version, v.payload, :scope, "
        "v.model_policy_id, v.execution_config_id, v.evaluation_policy_id, v.id, '{scope}', :t "
        "FROM settings_versions v WHERE v.id = :v",
        scope=scope,
        t=s.at(),
        v=active.settings_version_id,
    )


@dataclass(frozen=True)
class _Collection:
    run_id: int
    adzuna_instance: int
    ats_instance: int
    snapshot_id: int
    linked_item: int
    response_sha: bytes


async def _collection(s: _Seeder, active: _Active) -> _Collection:
    """The watchlist board and its instance, a collect run and its attempt, a cell with an interval, a quota
    window with its ledger entry, a fetch request with its response and raw items, an ATS snapshot."""
    company = await s.row(
        "watchlist_companies",
        {
            "display_name": "Seed Board",
            "company_key": "seed gmbh",
            "ats_type": "greenhouse",
            "board_slug": "seedboard",
            "detection": "manual",
            "validation_status": "ok",
            "validated_at": s.at(),
            "created_at": s.at(),
        },
    )
    ats = await s.row(
        "source_instances",
        {"source_id": "greenhouse", "instance_key": "seedboard", "watchlist_company_id": company},
    )
    found = await s.conn.execute(
        q("SELECT id FROM source_instances WHERE source_id = 'adzuna' AND instance_key = 'default'")
    )
    adzuna = int(found.scalar_one())
    query = await s.row(
        "queries", {"text": "erfundene rolle", "lang": "de", "origin": "user", "created_at": s.at()}
    )
    run = await s.row(
        "runs",
        {
            "trigger": "manual",
            "status": "succeeded",
            "include_paid": False,
            "settings_version_id": active.settings_version_id,
            "scope_version_id": active.scope_version_id,
            "attempt_no": 1,
            "queued_at": s.at(),
            "started_at": s.at(1),
            "heartbeat_at": s.at(50),
            "finished_at": s.at(60),
            "report": {},
        },
    )
    await s.row(
        "run_attempts",
        {
            "run_id": run,
            "attempt_no": 1,
            "worker_epoch": 1,
            "thread_id": f"run-{run}-a1",
            "started_at": s.at(1),
            "ended_at": s.at(60),
            "end_reason": "completed",
        },
        returning=False,
    )
    cell = await s.row(
        "coverage_cells",
        {
            "source_instance_id": adzuna,
            "query_id": query,
            "last_attempted_at": s.at(2),
            "last_covered_at": s.at(2),
        },
    )
    await s.update(
        "INSERT INTO coverage_intervals (cell_id, kind, span, run_id, created_at) "
        "VALUES (:cell, 'covered', tstzrange(:a, :b), :run, :b)",
        cell=cell,
        a=s.at(days=-1),
        b=s.at(),
        run=run,
    )
    window_start = s.at(-60)
    await s.row(
        "quota_windows",
        {
            "provider": "adzuna",
            "window_start": window_start,
            "window_end": s.at(days=1),
            "is_current": False,
            "closed_reason": "rolled_over",
            "limit_units": 250,
            "reserve_units": 10,
            "state": "validated",
            "tokens": Decimal(1),
            "tokens_at": s.at(),
            "capacity": Decimal(10),
        },
        returning=False,
    )
    request_key = s.uniq("request-key")
    ledger = await s.row(
        "quota_ledger",
        {
            "provider": "adzuna",
            "window_start": window_start,
            "kind": "request",
            "request_class": "normal",
            "request_key": request_key,
            "attempt_seq": 1,
            "run_id": run,
            "run_attempt_no": 1,
            "units": 1,
            "state": "billed",
            "http_status": 200,
            "per_run_cap": 10,
            "created_at": s.at(2),
            "dispatched_at": s.at(2),
            "settled_at": s.at(3),
        },
    )
    request = await s.row(
        "fetch_requests",
        {
            "run_id": run,
            "run_attempt_no": 1,
            "cell_id": cell,
            "source_instance_id": adzuna,
            "phase": "primary",
            "operation": "list",
            "request_key": request_key,
            "page_no": 1,
            "request_class": "normal",
            "spec": {},
            "status": "dispatched",
            "quota_ledger_id": ledger,
            "created_at": s.at(2),
            "dispatched_at": s.at(2),
        },
    )
    await s.update("UPDATE quota_ledger SET fetch_request_id = :r WHERE id = :l", r=request, l=ledger)
    # A stand-in body stored as `receipts.store_response` stores one: a zstd frame and the body's sha256,
    # which a restore's verification checks (M5 Task 26).
    body = s.uniq("body")
    response_sha = hashlib.sha256(body).digest()
    response = await s.row(
        "raw_responses",
        {
            "fetch_request_id": request,
            "ledger_entry_id": ledger,
            "source_instance_id": adzuna,
            "http_status": 200,
            "headers": {},
            "body_zstd": zstandard.ZstdCompressor(level=3).compress(body),
            "body_sha256": response_sha,
            "item_count": 2,
            "parsed_at": s.at(3),
            "fetched_at": s.at(3),
            "expires_at": s.at(days=30),
        },
    )
    await transition(
        s.conn,
        "fetch_requests",
        request,
        ["dispatched"],
        "succeeded",
        set_={"raw_response_id": response, "http_status": 200, "items": 2},
    )
    await s.update("UPDATE fetch_requests SET finished_at = :t WHERE id = :r", t=s.at(3), r=request)
    linked = await s.row(
        "raw_items",
        {
            "raw_response_id": response,
            "item_index": 0,
            "native_id": "seed-adz-1",
            "item_sha256": s.uniq("item"),
            "disposition": "linked",
            "created_at": s.at(3),
            "disposed_at": s.at(4),
        },
    )
    await s.row(
        "raw_items",
        {
            "raw_response_id": response,
            "item_index": 1,
            "native_id": "seed-adz-x",
            "item_sha256": s.uniq("item"),
            "disposition": "out_of_scope",
            "reason_code": "title_filtered",
            "created_at": s.at(3),
            "disposed_at": s.at(4),
        },
    )
    snapshot = await s.row(
        "ats_snapshots",
        {
            "source_instance_id": ats,
            "run_id": run,
            "status": "complete",
            "member_count": 1,
            "baseline_active_count": 2,
            "absent_count": 1,
            "taken_at": s.at(days=1),
            "applied_at": s.at(days=1),
        },
    )
    await s.row("ats_snapshot_members", {"snapshot_id": snapshot, "native_id": "seed-gh-2"}, returning=False)
    await s.update(
        "UPDATE source_instances SET last_complete_snapshot_id = :s, last_snapshot_active_count = 1, "
        "last_success_at = :t WHERE id = :i",
        s=snapshot,
        t=s.at(days=1),
        i=ats,
    )
    return _Collection(run, adzuna, ats, snapshot, linked, response_sha)


# ---- the jobs ----------------------------------------------------------------------------------------------


async def _job(
    s: _Seeder,
    label: str,
    *,
    availability: str = "closed",
    revisions: int = 1,
    merged_into: int | None = None,
    embedded: bool = False,
) -> _Job:
    """A job first seen at the seed's time, its availability changed a day later, with `revisions`
    revisions (the last current; embedded with `embedded`) and their occurrences."""
    job = await s.row(
        "jobs",
        {
            "company_key": "seed gmbh",
            "company_display": "Seed GmbH",
            "title": f"Erfundene Rolle {label}",
            "title_norm": f"erfundene rolle {label}",
            "location_kind": "unknown",
            "work_mode_possible": ["remote", "hybrid", "onsite"],
            "work_mode_source": "none",
            "employment_source": "none",
            "ad_language": "de",
            "completeness": "full",
            "posted_precision": "unknown",
            "first_seen_at": s.at(),
            "last_seen_at": s.at(days=1),
            "availability": availability,
            "availability_changed_at": s.at(days=1),
            "source_ids": ["adzuna"],
            "field_provenance": {},
            "det_facts_hash": s.uniq("facts"),
            "merged_into_job_id": merged_into,
            "updated_at": s.at(days=1),
        },
        casts={"work_mode_possible": "text[]", "source_ids": "text[]"},
    )
    made: list[int] = []
    for number in range(1, revisions + 1):
        last = number == revisions
        embedding = await _embedding(s, "passage") if embedded and last else None
        revision = await s.row(
            "job_revisions",
            {
                "job_id": job,
                "revision_no": number,
                "material_hash": s.uniq("material"),
                "llm_input": {"title": f"Erfundene Rolle {label}"},
                "description_text": "Erfundene Testanzeige.",
                "description_completeness": "full",
                "ad_language": "de",
                "embedding_status": "awaiting_embedding" if embedding is None else "embedded",
                "embedding_id": embedding,
                "created_at": s.at(number),
            },
        )
        await s.row(
            "job_revision_occurrences",
            {"job_id": job, "job_revision_id": revision, "became_current_at": s.at(number)},
            returning=False,
        )
        made.append(revision)
    await s.update("UPDATE jobs SET current_revision_id = :r WHERE id = :j", r=made[-1], j=job)
    return _Job(job, tuple(made))


async def _posting(
    s: _Seeder, job: _Job, instance: int, native_id: str, *, availability: str, raw_item: int | None = None
) -> tuple[int, int]:
    """A posting of the job with one observation, its latest; `(posting_id, observation_id)`."""
    closed = availability == "closed"
    posting = await s.row(
        "postings",
        {
            "source_instance_id": instance,
            "native_id": native_id,
            "job_id": job.id,
            "first_seen_at": s.at(),
            "last_seen_at": s.at(days=1),
            "seen_count": 2,
            "availability": availability,
            "availability_changed_at": s.at(days=1),
            "availability_reason": "expired" if closed else None,
            "outbound_url_private": None,
            "list_item_sha256": s.uniq("list-item"),
        },
    )
    seen = normalized(native_id)  # a whole posting, so a source purge can rebuild the job from it (Task 15)
    observation = await s.row(
        "posting_observations",
        {
            "posting_id": posting,
            "raw_item_id": raw_item,
            "source_instance_id": instance,
            "raw_response_sha256": s.uniq("response"),
            "observed_at": s.at(),
            "normalized": seen.model_dump(mode="json"),
            "content_hash": s.uniq("content"),
            "completeness": seen.description_completeness,
            "quality": seen.description_quality,
        },
    )
    await s.update("UPDATE postings SET latest_observation_id = :o WHERE id = :p", o=observation, p=posting)
    if raw_item is not None:
        await s.update("UPDATE raw_items SET observation_id = :o WHERE id = :i", o=observation, i=raw_item)
    return posting, observation


async def _key(s: _Seeder, key: AdKey, owner: _Job, sources: list[tuple[int, int, int]]) -> None:
    """The key owned by `owner`, supplied by each `(posting, observation, minutes)` source."""
    await s.row(
        "ad_keys",
        {
            "key_ns": key.ns,
            "key_value": key.value,
            "exclusive": key.exclusive,
            "job_id": owner.id,
            "created_at": s.at(),
        },
        returning=False,
    )
    for posting, observation, minutes in sources:
        await s.row(
            "ad_key_sources",
            {
                "key_ns": key.ns,
                "key_value": key.value,
                "posting_id": posting,
                "observation_id": observation,
                "first_seen_at": s.at(minutes),
            },
            returning=False,
        )


async def _actions(s: _Seeder, job: _Job, **stamps: datetime | str) -> None:
    await s.row("user_job_actions", {"job_id": job.id, **stamps, "updated_at": s.at(days=1)}, returning=False)


async def _event(s: _Seeder, kind: str, from_job: _Job, into_job: _Job, minutes: int) -> int:
    return await s.row(
        "merge_events",
        {
            "kind": kind,
            "from_job_id": from_job.id,
            "into_job_id": into_job.id,
            "posting_ids": [],
            "evidence": {"rule": "seed"},
            "created_at": s.at(minutes),
        },
        casts={"posting_ids": "bigint[]"},
    )


async def _conflict(
    s: _Seeder, posting: int, posting_job: _Job, candidates: list[_Job], *, open_: bool
) -> int:
    return await s.row(
        "identity_conflicts",
        {
            "posting_id": posting,
            "posting_job_id": posting_job.id,
            "candidate_job_ids": [job.id for job in candidates],
            "evidence": {"keys": ["seed"]},
            "status": "open" if open_ else "resolved_separate",
            "created_at": s.at(30),
            "resolved_at": None if open_ else s.at(days=1),
        },
        casts={"candidate_job_ids": "bigint[]"},
    )


async def _alert(s: _Seeder, code: str, subject: str, *, resolved: bool, severity: str | None = None) -> int:
    return await s.row(
        "alerts",
        {
            "code": code,
            "severity": severity or ("info" if code.startswith("dedup.") else "warn"),
            "dedup_key": f"{code}:{subject}",
            "message": f"seeded {code}",
            "context": {},
            "first_at": s.at(),
            "last_at": s.at(),
            "resolved_at": s.at(60) if resolved else None,
        },
    )


# ---- scoring -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Scoring:
    active: _Active
    profile: _Profile
    cycle: int
    bulk_done: int
    bulk_live: int


async def _target(
    s: _Seeder, sc: _Scoring, job: _Job, revision: int, llm_tuple: int, state: str, **columns: object
) -> int:
    values: dict[str, object] = {
        "job_revision_id": revision,
        "llm_tuple_id": llm_tuple,
        "job_id": job.id,
        "state": state,
        "state_changed_at": s.at(days=1),
        "created_at": s.at(10),
        "updated_at": s.at(days=1),
    }
    if state in REASON_OF:  # `cancelled`'s reason is the caller's (`state_reason=`)
        values["state_reason"] = REASON_OF[state]
    if state == "refused":
        values["refusal_category"] = "general_harms"
    if state == "filtered_out":
        values["filter_scope_version_id"] = sc.active.scope_version_id
    values.update(columns)
    return await s.row("score_targets", values)


async def _attempt(
    s: _Seeder, sc: _Scoring, target: int, outcome: str, *, mode: str = "online", **columns: object
) -> int:
    running = outcome == "running"
    values: dict[str, object] = {
        "target_id": target,
        "attempt_no": 1,
        "mode": mode,
        "cycle_id": sc.cycle if mode == "online" else None,
        "execution_config_id": sc.active.execution_config_id,
        "evaluation_policy_id": sc.active.evaluation_policy_id,
        "lane": "batch" if mode == "batch" else "similarity",
        "outcome": outcome,
        "logical_calls": 0 if running else 1,
        "http_calls": 0 if running else 1,
        "charged": not running,
        "started_at": s.at(20),
        "ended_at": None if running else s.at(25),
        **columns,
    }
    return await s.row("score_attempts", values)


async def _published(
    s: _Seeder,
    sc: _Scoring,
    job: _Job,
    revision: int,
    llm_tuple: int,
    profile_version: int,
    *,
    contested: bool,
) -> int:
    """A published target of the revision with its attempt, calls, drafts, critic note (when contested),
    result, evidence and instances (current and superseded); the result's id."""
    result = await s.next_id("llm_results")
    state = "published_contested" if contested else "published_accepted"
    target = await _target(
        s, sc, job, revision, llm_tuple, state, published_result_id=result, attempt_no=1, charged_attempts=1
    )
    attempt = await _attempt(s, sc, target, state)
    call = await _call(s, sc.active, purpose="generate", minutes=21, attempt_id=attempt, target_id=target)
    draft = await s.row(
        "score_drafts",
        {
            "attempt_id": attempt,
            "round": 0,
            "llm_call_id": call,
            "output_json": {},
            "validation": {},
            "provisional": {},
            "created_at": s.at(21),
        },
    )
    critic = await _call(s, sc.active, purpose="critic", minutes=22, attempt_id=attempt, target_id=target)
    await s.row(
        "critic_notes",
        {
            "attempt_id": attempt,
            "round": 1,
            "draft_id": draft,
            "llm_call_id": critic,
            "effort": "medium",
            "triggers": ["seed"],
            "verdict_returned": "revise" if contested else "accept",
            "derived_outcome": "incomplete" if contested else "accept",
            "review_json": {},
            "validation": {},
            "created_at": s.at(22),
        },
        casts={"triggers": "text[]"},
    )
    await s.row(
        "llm_results",
        {
            "id": result,
            "target_id": target,
            "attempt_id": attempt,
            "final_draft_id": draft,
            "job_revision_id": revision,
            "profile_version_id": profile_version,
            "rubric_version_id": sc.active.rubric_version_id,
            "prompt_schema_version_id": sc.active.prompt_schema_version_id,
            "model_policy_id": sc.active.model_policy_id,
            "evaluation_policy_id": sc.active.evaluation_policy_id,
            "status": "contested" if contested else "accepted",
            "criteria": {},
            "job_facts": {},
            "knockout_candidates": {},
            "fact_confirmations": {},
            "not_a_job_ad": False,
            "injection_suspected": False,
            "nulled_criteria": ["skills"] if contested else [],
            "critic_outcome": "contested" if contested else "accepted",
            "critic_injection": "none",
            "revisions_used": 0,
            "summary_en": "Invented summary.",
            "summary_de": "Erfundene Zusammenfassung.",
            "published_at": s.at(25),
        },
        casts={"nulled_criteria": "text[]"},
        overriding=True,
        returning=False,
    )
    await s.row(
        "result_evidence",
        {
            "llm_result_id": result,
            "subject": "criterion:skills",
            "source_kind": "description",
            "quote": "Erfundene Testanzeige.",
            "span_start": 0,
            "span_end": 10,
        },
    )
    for current in (False, True):
        await s.row(
            "det_results",
            {
                "llm_result_id": result,
                "scope_version_id": sc.active.scope_version_id,
                "job_id": job.id,
                "det_algorithm_version": "det-seed",
                "is_current": current,
                "superseded_at": None if current else s.at(26),
                "det_criteria": {},
                "knockouts": {},
                "industry_match": "neutral",
                "caps_applied": {},
                "score_low": Decimal("60.00"),
                "score_mid": Decimal("70.00"),
                "score_high": Decimal("80.00"),
                "coverage": Decimal("0.9"),
                "confidence": "high",
                "det_inputs": {},
                "det_inputs_hash": s.uniq("det"),
                "computed_at": s.at(26 if not current else 27),
            },
        )
    return result


async def _ended(
    s: _Seeder, sc: _Scoring, job: _Job, revision: int, llm_tuple: int, state: str, **columns: object
) -> int:
    """A target that ended in `state` after one online attempt (its generate call billed)."""
    target = await _target(
        s, sc, job, revision, llm_tuple, state, attempt_no=1, charged_attempts=1, **columns
    )
    outcome = {
        "refused": "refused",
        "failed_max_tokens": "failed_max_tokens",
        "failed_invalid_output": "failed_invalid_output",
        "failed_request": "failed_request",
        "failed_transient": "transient_error",
        "failed_attempts_exhausted": "transient_error",
        "expired_unscored": "budget_exhausted",
        "cancelled": "cancelled",
    }[state]
    attempt = await _attempt(s, sc, target, outcome)
    await _call(s, sc.active, purpose="generate", minutes=21, attempt_id=attempt, target_id=target)
    return target


async def _submission(s: _Seeder, sc: _Scoring, state: str, *, bulk: int, operation: int, items: int) -> int:
    """A batch submission in `state` with the columns its CHECKs need at that state."""
    started = state not in ("preparing", "abandoned")
    provider = state in ("submitted", "ended", "reconciled", "released")
    return await s.row(
        "batch_submissions",
        {
            "state": state,
            "llm_tuple_id": sc.profile.active_tuple,
            "bulk_operation_id": bulk,
            "operation_id": operation,
            "request_count": items,
            "request_hash": s.uniq("batch"),
            "provider_batch_id": f"msgbatch_seed_{state}" if provider else None,
            "reserved_usd": Decimal("0.1") * items,
            "followup_held_usd": Decimal(0),
            "submit_started_at": s.at(30) if started else None,
            "submitted_at": s.at(31) if provider else None,
            "expires_at": s.at(31, days=1) if provider else None,
            "ended_at": s.at(90) if state in ("ended", "reconciled") else None,
            "results_complete_at": s.at(95) if state == "reconciled" else None,
            "reconciled_at": s.at(100) if state == "reconciled" else None,
            "released_at": s.at(days=2) if state == "released" else None,
            "created_at": s.at(29),
        },
    )


async def _batched(
    s: _Seeder,
    sc: _Scoring,
    submission: int,
    target: int,
    *,
    attempt_outcome: str,
    entry_state: str,
    bulk: int,
    result_type: str = "awaiting",
    decision: str | None = None,
    hold_released: bool = False,
    evidence: str | None = None,
) -> None:
    """The target's batch attempt, its item in the submission and the item's call and entry."""
    charged = entry_state not in ("reserved", "not_billed")
    attempt = await _attempt(s, sc, target, attempt_outcome, mode="batch", charged=charged)
    item = await s.row(
        "batch_items",
        {
            "submission_id": submission,
            "target_id": target,
            "attempt_no": 1,
            "custom_id": f"t{target}_a1",
            "result_type": result_type,
            "decision": decision,
            "processed_at": None if decision is None else s.at(92),
            "hold_usd": Decimal(0),
            "hold_released_at": s.at(days=2) if hold_released else None,
        },
    )
    call = await _call(
        s,
        sc.active,
        purpose="generate",
        entry_state=entry_state,
        transport="batch",
        minutes=30,
        bulk_operation_id=bulk,
        evidence=evidence,
        attempt_id=attempt,
        target_id=target,
        batch_item_id=item,
    )
    await s.update("UPDATE batch_items SET llm_call_id = :c WHERE id = :i", c=call, i=item)


async def _operation(s: _Seeder, kind: str, status: str, label: str, payload: dict[str, Any]) -> int:
    done = status in ("succeeded", "failed", "cancelled")
    return await s.row(
        "operations",
        {
            "kind": kind,
            "payload": payload,
            "dedup_key": f"{kind}:{label}",
            "status": status,
            "attempts": 1 if done else 0,
            "created_at": s.at(28),
            "started_at": s.at(29) if done else None,
            "finished_at": s.at(days=1) if done else None,
        },
    )


# ---- the seed ----------------------------------------------------------------------------------------------


async def seed_two_year_old_graph(
    conn: AsyncConnection, *, now: datetime, live_submission: InFlight = "submitted"
) -> SeedIds:
    """Write the seed (module docstring) in the caller's transaction; the ids its tests read."""
    s = _Seeder(conn, now, live_submission)
    active = await _active(s)
    await _ops_rows(s)
    await _configuration(s, active)
    await s.update(
        "INSERT INTO llm_budget (month, cap_usd, overrun_buffer_usd, bucket_usd, bucket_at, updated_at) "
        "VALUES (:m, 100, 2, 1, :t, :t)",
        m=s.month,
        t=s.at(),
    )
    purgeable = await _purgeable_profile(s, active)  # confirmed first: its draft is the oldest
    profile = await _profiles(s, active)
    coll = await _collection(s, active)

    # the jobs
    closed = await _job(s, "closed", revisions=2, embedded=True)
    merged = await _job(s, "merged", merged_into=closed.id)
    deep = await _job(s, "merged-deep", merged_into=merged.id)
    active_job = await _job(s, "active", availability="active")
    other = await _job(s, "other", availability="active")
    member = await _job(s, "member", availability="active")
    deferred = await _job(s, "deferred", availability="active")
    saved = await _job(s, "saved")
    applied = await _job(s, "applied", availability="stale")
    golden = await _job(s, "golden")
    in_progress = await _job(s, "in-progress")
    batched = [await _job(s, f"batched-{n}") for n in (1, 2)]
    filtered = await _job(s, "filtered")
    failed = await _job(s, "failed")
    exhausted = await _job(s, "exhausted", availability="stale")

    # postings, observations, keys, lineage
    p_adz, o_adz = await _posting(
        s,
        closed,
        coll.adzuna_instance,
        CLOSED_ADZUNA_KEY.value,
        availability="closed",
        raw_item=coll.linked_item,
    )
    p_ats, o_ats = await _posting(s, closed, coll.ats_instance, "seed-gh-1", availability="closed")
    await s.update(
        "UPDATE postings SET absent_snapshots = 1, first_absent_at = :t, last_absent_snapshot_id = :s "
        "WHERE id = :p",
        t=s.at(days=1),
        s=coll.snapshot_id,
        p=p_ats,
    )
    await s.row(
        "ats_snapshot_absences", {"snapshot_id": coll.snapshot_id, "posting_id": p_ats}, returning=False
    )
    p_act, o_act = await _posting(s, active_job, coll.adzuna_instance, "seed-adz-2", availability="active")
    p_other, o_other = await _posting(s, other, coll.adzuna_instance, "seed-adz-3", availability="active")
    await _key(s, CLOSED_ADZUNA_KEY, closed, [(p_adz, o_adz, 0)])
    await _key(s, AdKey("ats:greenhouse:seedboard", "seed-gh-1"), closed, [(p_ats, o_ats, 0)])
    await _key(s, SHARED_KEY, closed, [(p_adz, o_adz, 0), (p_act, o_act, 60), (p_other, o_other, 120)])
    await _key(s, AdKey("adzuna", "seed-adz-2"), active_job, [(p_act, o_act, 60)])
    await _key(s, AdKey("adzuna", "seed-adz-3"), other, [(p_other, o_other, 120)])
    for field_name, source, posting, observation in (
        ("title", "adzuna", p_adz, o_adz),
        ("description", "greenhouse", p_ats, o_ats),
    ):
        await s.row(
            "job_revision_sources",
            {
                "job_revision_id": closed.current,
                "field": field_name,
                "source_id": source,
                "posting_id": posting,
                "observation_id": observation,
            },
            returning=False,
        )
    for job, passed in ((closed, True), (filtered, False)):
        await s.row(
            "hard_filter_results",
            {
                "job_id": job.id,
                "scope_version_id": active.scope_version_id,
                "passed": passed,
                "reasons": [] if passed else ["location"],
                "inputs_hash": s.uniq("inputs"),
                "det_algorithm_version": "det-seed",
                "computed_at": s.at(days=1),
            },
            casts={"reasons": "text[]"},
            returning=False,
        )

    # Task 15: an Adzuna-only job with a job merged into it (the merge event names both), each revision
    # drawing on Adzuna (lineage)
    adzuna_root = await _job(s, "adzuna-root", availability="active")
    adzuna_merged = await _job(s, "adzuna-merged", availability="active", merged_into=adzuna_root.id)
    p_root, o_root = await _posting(s, adzuna_root, coll.adzuna_instance, "seed-adz-5", availability="active")
    await _key(s, AdKey("adzuna", "seed-adz-5"), adzuna_root, [(p_root, o_root, 30)])
    for revision in (adzuna_root.current, adzuna_merged.current):  # the merged job's posting moved
        await s.row(
            "job_revision_sources",
            {
                "job_revision_id": revision,
                "field": "title",
                "source_id": "adzuna",
                "posting_id": p_root,
                "observation_id": o_root,
            },
            returning=False,
        )
    await _event(s, "strong_key_merge", adzuna_merged, adzuna_root, 45)

    # user actions, the older tombstone, the group, merges, decisions, conflicts
    await _actions(s, closed, seen_at=s.at(60), hidden_at=s.at(120), note="Erfundene Notiz.")
    await _actions(s, active_job, seen_at=s.at(60))
    await _actions(s, saved, seen_at=s.at(60), saved_at=s.at(90))
    await _actions(s, applied, seen_at=s.at(60), applied_at=s.at(90))
    old_tombstone = await s.row(
        "job_tombstones",
        {
            "former_job_id": 9_000_000_000_001,
            "user_actions": {"hidden_at": s.at().isoformat()},
            "tombstoned_at": s.at(),
        },
    )
    await s.row(
        "tombstone_keys",
        {"key_hash": tombstone_key_hash(AdKey("adzuna", "seed-old-1")), "tombstone_id": old_tombstone},
        returning=False,
    )
    await s.update(
        "UPDATE jobs SET restored_from_tombstone_id = :t WHERE id = :j", t=old_tombstone, j=other.id
    )
    group = await s.row(
        "duplicate_groups",
        {
            "representative_job_id": closed.id,
            "status": "active",
            "election_reason": "initial",
            "elected_at": s.at(5),
            "created_at": s.at(5),
            "updated_at": s.at(5),
        },
    )
    for job in (closed, member, deferred):
        await s.row(
            "duplicate_group_members",
            {"group_id": group, "job_id": job.id, "joined_at": s.at(5), "evidence": {"rule": "seed"}},
            returning=False,
        )
        await s.update("UPDATE jobs SET duplicate_group_id = :g WHERE id = :j", g=group, j=job.id)
    await s.row(
        "duplicate_groups",
        {
            "status": "dissolved",
            "election_reason": "member_left",
            "elected_at": s.at(4),
            "created_at": s.at(4),
            "updated_at": s.at(6),
        },
    )
    await _event(s, "strong_key_merge", deep, merged, 40)
    await _event(s, "strong_key_merge", merged, closed, 50)
    reversed_merge = await _event(s, "strong_key_merge", other, closed, 120)
    unmerge = await _event(s, "unmerge", other, closed, 180)
    await s.update(
        "UPDATE merge_events SET reversed_by_event_id = :u WHERE id = :e", u=unmerge, e=reversed_merge
    )
    # a decision on each side of the due graph: `closed_job` the lower id of one, `filtered` the higher of the
    # other (each of `dedup_decisions`' two keys then references the deleted graph)
    for first, second, source in ((closed, other, "unmerge"), (member, filtered, "user_split")):
        low, high = sorted((first.id, second.id))
        await s.row(
            "dedup_decisions",
            {"job_a": low, "job_b": high, "decision": "different", "source": source, "decided_at": s.at(180)},
            returning=False,
        )
    await _conflict(s, p_adz, closed, [active_job], open_=False)
    lone = await _conflict(s, p_act, active_job, [closed], open_=True)
    shared = await _conflict(s, p_other, other, [closed, active_job], open_=True)
    await _alert(s, "dedup.identity_conflict", f"conflict:{lone}", resolved=False)
    await _alert(s, "dedup.ambiguous_grouping", f"job:{closed.id}", resolved=False)
    resolved_alert = await _alert(s, "retention.failed", "seed", resolved=True)
    # Task 16: an open warn alert never pushed, and the bound violation that pauses the score lane until it is
    # acknowledged (`scoring.cycles.pause_reason`)
    open_alert = await _alert(s, "operation.failed", "operation:9000000000002", resolved=False)
    bound_alert = await _alert(
        s, "llm.reservation_bound_violated", _MODEL, resolved=False, severity="critical"
    )

    # scoring
    cycle = await s.row(
        "score_cycles",
        {
            "trigger": "periodic",
            "llm_tuple_id": profile.active_tuple,
            "scope_version_id": active.scope_version_id,
            "evaluation_policy_id": active.evaluation_policy_id,
            "worker_epoch": 1,
            "status": "completed",
            "bucket_usd_at_start": Decimal(1),
            "c_hat_usd": Decimal("0.05"),
            "admission_size": 3,
            "report": {},
            "started_at": s.at(20),
            "heartbeat_at": s.at(28),
            "finished_at": s.at(29),
        },
    )
    live_items = len(batched)
    bulk_done = await s.row(
        "bulk_operations",
        {
            "kind": "tuple_rescore",
            "llm_tuple_id": profile.active_tuple,
            "state": "completed",
            "budget_usd": Decimal(5),
            "spent_usd": Decimal("0.3"),
            "created_at": s.at(28),
            "finished_at": s.at(days=2),
        },
    )
    bulk_live = await s.row(
        "bulk_operations",
        {
            "kind": "tuple_rescore",
            "llm_tuple_id": profile.active_tuple,
            "state": "active",
            "budget_usd": Decimal(5),
            "spent_usd": Decimal("0.1") * live_items,
            "created_at": s.at(28),
        },
    )
    await s.row(
        "admission_lane_credits",
        {"llm_tuple_id": profile.active_tuple, "lane": "similarity", "credit": Decimal("0.25")},
        returning=False,
    )
    sc = _Scoring(active, profile, cycle, bulk_done, bulk_live)
    act, old = profile.active_tuple, profile.old_tuple

    # every target state: the full graph's, then the state jobs'
    closed_result = await _published(
        s, sc, closed, closed.current, act, profile.active_version, contested=False
    )
    old_result = await _published(s, sc, closed, closed.current, old, profile.old_version, contested=True)
    await _ended(s, sc, closed, closed.revisions[0], act, "cancelled", state_reason="revision_superseded")
    await _ended(s, sc, closed, closed.revisions[0], old, "refused")
    await _ended(s, sc, merged, merged.current, act, "failed_max_tokens")
    await _ended(s, sc, merged, merged.current, old, "expired_unscored")
    await _ended(s, sc, deep, deep.current, act, "failed_transient")
    await _target(s, sc, deferred, deferred.current, act, "deferred_duplicate", deferred_group_id=group)
    await _target(s, sc, member, member.current, act, "budget_deferred")
    await _target(s, sc, filtered, filtered.current, act, "filtered_out")
    golden_result = await _published(
        s, sc, golden, golden.current, act, profile.active_version, contested=False
    )
    # the purgeable version's tuple: a published target and a cancelled one, both ended two years ago
    purgeable_result = await _published(
        s, sc, closed, closed.revisions[0], purgeable.tuple_id, purgeable.version, contested=False
    )
    await _ended(
        s,
        sc,
        active_job,
        active_job.current,
        purgeable.tuple_id,
        "cancelled",
        state_reason="tuple_superseded",
    )

    claim = uuid4()
    claimed = await _target(
        s,
        sc,
        in_progress,
        in_progress.current,
        act,
        "in_progress",
        claim_id=claim,
        claim_epoch=1,
        claimed_at=s.at(20),
        claim_heartbeat_at=s.at(21),
        attempt_no=1,
        lane="similarity",
    )
    attempt = await _attempt(s, sc, claimed, "running", claim_id=claim, claim_epoch=1)
    dispatched_call = await _call(
        s,
        sc.active,
        purpose="generate",
        entry_state="dispatched",
        minutes=21,
        attempt_id=attempt,
        target_id=claimed,
    )
    # Task 16: another online try in flight, its call still `reserved` (an active job's claimed target)
    in_flight = await _job(s, "in-flight", availability="active")
    reserving_claim = uuid4()
    reserving = await _target(
        s,
        sc,
        in_flight,
        in_flight.current,
        act,
        "in_progress",
        claim_id=reserving_claim,
        claim_epoch=1,
        claimed_at=s.at(22),
        claim_heartbeat_at=s.at(23),
        attempt_no=1,
        lane="similarity",
    )
    reserving_attempt = await _attempt(s, sc, reserving, "running", claim_id=reserving_claim, claim_epoch=1)
    reserved_call = await _call(
        s,
        sc.active,
        purpose="generate",
        entry_state="reserved",
        minutes=23,
        attempt_id=reserving_attempt,
        target_id=reserving,
    )

    # the live submission and its `in_batch` targets
    preparing = live_submission == "preparing"
    live_operation = await _operation(
        s,
        "batch_submit",
        "pending" if live_submission in ("preparing", "submit_started") else "succeeded",
        "live",
        {},
    )
    live = await _submission(
        s, sc, live_submission, bulk=bulk_live, operation=live_operation, items=live_items
    )
    for job in batched:
        target = await _target(
            s,
            sc,
            job,
            job.current,
            act,
            "in_batch",
            batch_submission_id=live,
            attempt_no=1,
            charged_attempts=0 if preparing else 1,
            lane="batch",
            bulk_operation_id=bulk_live,
        )
        await _batched(
            s,
            sc,
            live,
            target,
            attempt_outcome="running",
            entry_state="reserved" if preparing else "dispatched",
            bulk=bulk_live,
        )

    # the terminal submissions
    done_operation = await _operation(s, "batch_submit", "succeeded", "done", {})
    reconciled = await _submission(s, sc, "reconciled", bulk=bulk_done, operation=done_operation, items=1)
    target = await _target(
        s,
        sc,
        failed,
        failed.current,
        act,
        "failed_invalid_output",
        attempt_no=1,
        charged_attempts=1,
        lane="batch",
    )
    await _batched(
        s,
        sc,
        reconciled,
        target,
        attempt_outcome="failed_invalid_output",
        entry_state="billed",
        bulk=bulk_done,
        result_type="succeeded",
        decision="failed_invalid_output",
    )
    await _ended(s, sc, failed, failed.current, old, "failed_request")
    abandoned = await _submission(s, sc, "abandoned", bulk=bulk_done, operation=done_operation, items=1)
    target = await _target(s, sc, active_job, active_job.current, act, "pending", attempt_no=1)
    await _batched(
        s,
        sc,
        abandoned,
        target,
        attempt_outcome="cancelled",
        entry_state="not_billed",
        bulk=bulk_done,
        hold_released=True,
        evidence="never_dispatched",
    )
    released = await _submission(s, sc, "released", bulk=bulk_done, operation=done_operation, items=1)
    target = await _target(
        s,
        sc,
        exhausted,
        exhausted.current,
        act,
        "failed_attempts_exhausted",
        attempt_no=1,
        charged_attempts=1,
        lane="batch",
    )
    await _batched(
        s,
        sc,
        released,
        target,
        attempt_outcome="released",
        entry_state="ambiguous",
        bulk=bulk_done,
        hold_released=True,
    )

    # golden cases, the eval run, feedback
    golden_cases = []
    for revision, version, result, is_active in (
        (golden.current, profile.active_version, golden_result, True),
        (closed.current, profile.old_version, old_result, False),
        (closed.revisions[0], purgeable.version, purgeable_result, False),
    ):
        golden_cases.append(
            await s.row(
                "golden_cases",
                {
                    "job_revision_id": revision,
                    "profile_version_id": version,
                    "scope_version_id": active.scope_version_id,
                    "label": "good",
                    "comment": "Erfundener Kommentar.",
                    "source": "week1" if is_active else "feedback",
                    "labelled_from_result_id": result,
                    "det_inputs_snapshot": {},
                    "det_input_sources": ["adzuna"],
                    "active": is_active,
                    "created_at": s.at(days=1),
                    "updated_at": s.at(days=1),
                },
                casts={"det_input_sources": "text[]"},
            )
        )
    eval_run = await s.row(
        "eval_runs",
        {
            "kind": "golden",
            "route": "production",
            "rubric_version_id": active.rubric_version_id,
            "prompt_schema_version_id": active.prompt_schema_version_id,
            "model_policy_id": active.model_policy_id,
            "evaluation_policy_id": active.evaluation_policy_id,
            "execution_config_id": active.execution_config_id,
            "repetitions": 1,
            "status": "completed",
            "metrics": {"transport_requested": "fake"},
            "passed": True,
            "cost_usd": Decimal("0.1"),
            "started_at": s.at(days=1),
            "finished_at": s.at(60, days=1),
        },
    )
    # Task 16: a running eval run with a call in flight (`dispatched`), naming no case
    running_eval = await s.row(
        "eval_runs",
        {
            "kind": "golden",
            "route": "production",
            "rubric_version_id": active.rubric_version_id,
            "prompt_schema_version_id": active.prompt_schema_version_id,
            "model_policy_id": active.model_policy_id,
            "evaluation_policy_id": active.evaluation_policy_id,
            "execution_config_id": active.execution_config_id,
            "repetitions": 1,
            "status": "running",
            "metrics": {"transport_requested": "fake"},
            "cost_usd": Decimal(0),
            "started_at": s.at(days=2),
        },
    )
    eval_call = await _call(
        s,
        active,
        purpose="generate",
        entry_state="dispatched",
        minutes=60 * 48 + 5,
        eval_run_id=running_eval,
    )
    for case in golden_cases:
        call = await _call(
            s, active, purpose="generate", minutes=60 * 24 + 5, eval_run_id=eval_run, golden_case_id=case
        )
        await s.row(
            "eval_case_results",
            {
                "eval_run_id": eval_run,
                "golden_case_id": case,
                "repetition": 1,
                "outcome": "accepted",
                "critic_ran": False,
                "llm_call_ids": [call],
                "output": {},
                "score_mid": Decimal("70.00"),
            },
            casts={"llm_call_ids": "bigint[]"},
            returning=False,
        )
    det = await s.conn.execute(
        q("SELECT id FROM det_results WHERE llm_result_id = :r AND is_current"), {"r": closed_result}
    )
    await s.row(
        "score_feedback",
        {
            "job_id": closed.id,
            "job_revision_id": closed.current,
            "profile_version_id": profile.active_version,
            "llm_result_id": closed_result,
            "det_result_id": int(det.scalar_one()),
            "score_snapshot": {"score_mid": "70.00"},
            "kind": "label",
            "label": "good",
            "comment": "Erfundener Kommentar.",
            "created_at": s.at(days=1),
        },
    )

    # Task 16: a scheduled run that ended `succeeded` and was never pinged (`deadman_state`'s watermark NULL)
    unpinged = await s.row(
        "runs",
        {
            "trigger": "schedule",
            "scheduled_slot": s.at(days=2),
            "status": "succeeded",
            "include_paid": False,
            "settings_version_id": active.settings_version_id,
            "scope_version_id": active.scope_version_id,
            "attempt_no": 1,
            "queued_at": s.at(days=2),
            "started_at": s.at(1, days=2),
            "heartbeat_at": s.at(50, days=2),
            "finished_at": s.at(60, days=2),
            "report": {},
        },
    )

    # deletion manifests, fences, backups, exports, operations
    manifest_uuid = uuid4()
    recorded: ManifestContent = {
        "format": 1,
        "manifest_uuid": str(manifest_uuid),
        "kind": "golden_case_delete",
        "admitted_at": s.at(30).isoformat(),
        "fenced_at": s.at(30).isoformat(),
        "max_ids": {},
        "operations": [],
    }
    file_sha = hashlib.sha256(canonical_bytes(recorded)).digest()
    shipment = await s.row(
        "manifest_shipments", {"restic_snapshot_id": "seed-manifests-snapshot", "shipped_at": s.at(120)}
    )
    manifest = await s.row(
        "deletion_manifests",
        {
            "manifest_uuid": manifest_uuid,
            "kind": "golden_case_delete",
            "scope": recorded,
            "status": "shipped",
            "file_sha256": file_sha,
            "created_at": s.at(30),
            "completed_at": s.at(60),
            "shipped_at": s.at(120),
            "shipment_id": shipment,
            "request": {"golden_case_id": 9_000_000_000_001},
        },
    )
    await s.row(
        "manifest_shipment_items",
        {"shipment_id": shipment, "manifest_uuid": manifest_uuid, "file_sha256": file_sha},
        returning=False,
    )
    await s.row(
        "purge_fences",
        {
            "kind": "jobs",
            "job_ids": [9_000_000_000_001],
            "manifest_id": manifest,
            "created_at": s.at(30),
            "released_at": s.at(60),
        },
        casts={"job_ids": "bigint[]"},
    )
    await s.row(
        "purged_batch_items",
        {
            "submission_id": released,
            "custom_id": "t9000000000001_a1",
            "manifest_id": manifest,
            "purged_at": s.at(60),
        },
        returning=False,
    )
    await _operation(s, "purge", "succeeded", str(manifest_uuid), {"manifest_id": manifest})
    backup_set = await s.row(
        "backup_sets",
        {
            "status": "succeeded",
            "restic_snapshot_id": "seed-set-snapshot",
            "dump_sha256": s.uniq("dump"),
            "dump_bytes": 1024,
            "db_snapshot_at": s.at(240),
            "manifest_count": 1,
            "row_counts": {"jobs": 1},
            "cv_file_count": 2,
            "started_at": s.at(230),
            "finished_at": s.at(250),
        },
    )
    await s.row(
        "backup_runs",
        {
            "kind": "restore_test",
            "backup_set_id": backup_set,
            "status": "succeeded",
            "report": {},
            "started_at": s.at(260),
            "finished_at": s.at(290),
        },
    )
    await s.row("exports", {"status": "expired", "created_at": s.at(), "expires_at": s.at(days=7)})
    ready = await s.row(
        "exports",
        {
            "status": "ready",
            "sha256": s.uniq("export"),
            "size_bytes": 64,
            "created_at": s.at(),
            "expires_at": s.at(days=7),
        },
    )
    await s.update("UPDATE exports SET storage_path = :p WHERE id = :i", p=f"{ready}-seed.jhx", i=ready)

    # the month's counters, as its entries count
    await s.update(
        "UPDATE llm_budget SET scoring_counted_usd = :scoring, nonscoring_counted_usd = :nonscoring, "
        "counted_usd = :scoring + :nonscoring WHERE month = :m",
        scoring=s.counted["scoring"],
        nonscoring=s.counted["nonscoring"],
        m=s.month,
    )
    due = (closed, in_progress, *batched, filtered, failed, exhausted)
    return SeedIds(
        live_submission_id=live,
        closed_job_id=closed.id,
        due_job_ids=tuple(sorted(job.id for job in due)),
        merged_job_ids=(deep.id, merged.id),
        exempt_job_ids=(saved.id, applied.id, golden.id),
        active_job_id=active_job.id,
        lone_conflict_id=lone,
        shared_conflict_id=shared,
        active_version_id=profile.active_version,
        previous_version_id=profile.old_version,
        purgeable_version_id=purgeable.version,
        purgeable_tuple_id=purgeable.tuple_id,
        purgeable_draft_id=purgeable.draft,
        purgeable_cv_file_id=purgeable.cv_file,
        purgeable_case_id=golden_cases[2],
        closed_case_id=golden_cases[1],
        ready_proposal_id=purgeable.ready,
        failed_proposal_id=purgeable.failed,
        running_proposal_id=purgeable.running,
        running_operation_id=purgeable.operation,
        in_flight_call_id=purgeable.in_flight,
        purgeable_call_ids=tuple(sorted(purgeable.calls)),
        ready_export_id=ready,
        adzuna_only_job_ids=tuple(sorted((active_job.id, other.id, adzuna_root.id))),
        adzuna_merge_root_id=adzuna_root.id,
        adzuna_merged_job_id=adzuna_merged.id,
        golden_job_id=golden.id,
        batched_job_ids=tuple(job.id for job in batched),
        reserved_entry_id=await _ledger_of(s, reserved_call),
        dispatched_entry_id=await _ledger_of(s, dispatched_call),
        eval_entry_id=await _ledger_of(s, eval_call),
        running_eval_run_id=running_eval,
        unpinged_run_id=unpinged,
        open_alert_id=open_alert,
        bound_violation_alert_id=bound_alert,
        resolved_alert_id=resolved_alert,
    )


async def _ledger_of(s: _Seeder, call_id: int) -> int:
    found = await s.conn.execute(q("SELECT spend_ledger_id FROM llm_calls WHERE id = :c"), {"c": call_id})
    return int(found.scalar_one())
