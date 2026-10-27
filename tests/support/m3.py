"""Synthetic M3 rows for the schema, view and reset tests: ids, hashes, scores and invented text, no ad or CV
content.

`insert_row` and `insert_id` write one row of any table from a mapping of column values (a dict is bound as
JSONB, a list as an array). The named helpers fill every NOT NULL column of their table with a valid value,
so a test names only what it is about.

`insert_claimed` builds a target the way the claim leaves it, with its running attempt, for a scoring call
to name. `insert_published` builds a target the way publication leaves it: the target in
`published_accepted` (or `published_contested`) naming its result, the attempt, the attempt's scoring call,
the final draft and the `llm_results` row. The target and its result name each other through deferrable
foreign keys, and inserting the target in its final state needs no transition (the `score_targets` trigger
checks UPDATEs only), so the result id is taken from the identity sequence first and the result is inserted
with it (`OVERRIDING SYSTEM VALUE`).

`CLAIM_WHERE`, `CLAIM_FROM`, `CLAIM_SET_SQL` and `CLAIM_RETURNING` are the atomic claim's arguments to
`transition_where`, for the tests of the set-based helper.

`insert_m1_configuration` and `insert_m2_seed_rows` put an older install's state in place by direct SQL, for
the upgrade tests of migration 0008: no seed of this code runs against an older schema, because this code's
seed writes columns that schema does not have yet (ruling T1-b).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final
from uuid import UUID, uuid4

import psycopg
from jobhunter.db.seed import SETTINGS_SCHEMA_VERSION, load_settings_v1
from jobhunter.db.sql import q
from jobhunter.llm.prompts.render import prompt_schema_hash, prompt_schema_v1
from jobhunter.scoring.rubric import RUBRIC_V1, load_rubric
from psycopg.types.json import Jsonb
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from tests.support.pg import PgServer

NOW: Final = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
_BUDGET_MONTH: Final = date(2026, 10, 1)

# The atomic claim (§9.4, L3477-3488) as `transition_where` runs it, `pending -> in_progress`: the caller's
# `FOR UPDATE SKIP LOCKED` subquery in `where`, the admitted ids with their lanes and always-include reasons
# as the source relation `a`, and the claim columns as SQL over `t` and `a`. Its binds: `ids`, `lanes`,
# `reasons`, `epoch` and `max_attempts`.
CLAIM_WHERE: Final = (
    "t.id = a.id AND t.id IN (SELECT id FROM score_targets WHERE id = ANY(CAST(:ids AS bigint[])) "
    "AND state = 'pending' AND (not_before IS NULL OR not_before <= now()) "
    "AND charged_attempts < :max_attempts AND NOT is_fenced(job_id, llm_tuple_id) FOR UPDATE SKIP LOCKED)"
)
CLAIM_FROM: Final = (
    "unnest(CAST(:ids AS bigint[]), CAST(:lanes AS text[]), CAST(:reasons AS text[])) AS a(id, lane, reason)"
)
CLAIM_SET_SQL: Final = {
    "claim_id": "gen_random_uuid()",
    "claim_epoch": ":epoch",
    "claimed_at": "now()",
    "claim_heartbeat_at": "now()",
    "attempt_no": "t.attempt_no + 1",
    "lane": "a.lane",
    "always_include_reason": "a.reason",
}
CLAIM_RETURNING: Final = ("id", "claim_id", "attempt_no")


def _insert(table: str, values: Mapping[str, object], *, returning: bool, overriding: bool) -> TextClause:
    columns = ", ".join(values)
    binds = ", ".join(f":{name}" for name in values)
    override = " OVERRIDING SYSTEM VALUE" if overriding else ""
    tail = " RETURNING id" if returning else ""
    statement = f"INSERT INTO {table} ({columns}){override} VALUES ({binds}){tail}"  # noqa: S608 (test names)
    return q(statement, jsonb=[name for name, value in values.items() if isinstance(value, dict)])


async def insert_row(
    c: AsyncConnection, table: str, values: Mapping[str, object], *, overriding: bool = False
) -> None:
    """Insert one row of `table`; a dict value is bound as JSONB, a list as an array."""
    await c.execute(_insert(table, values, returning=False, overriding=overriding), dict(values))


async def insert_id(c: AsyncConnection, table: str, values: Mapping[str, object]) -> int:
    """`insert_row` for a table with an `id`; returns it."""
    row = await c.execute(_insert(table, values, returning=True, overriding=False), dict(values))
    return int(row.scalar_one())


@dataclass(frozen=True)
class Active:
    """The configuration rows `active_config` names."""

    scope_version_id: int
    evaluation_policy_id: int
    execution_config_id: int


_ACTIVE: Final = q(
    "SELECT scope_version_id, evaluation_policy_id, execution_config_id FROM active_config WHERE id = 1"
)


async def active_ids(c: AsyncConnection) -> Active:
    row = (await c.execute(_ACTIVE)).one()
    return Active(int(row.scope_version_id), int(row.evaluation_policy_id), int(row.execution_config_id))


@dataclass(frozen=True)
class Job:
    """An unmerged job and its current revision."""

    job_id: int
    revision_id: int


_JOB: Final = q(
    "INSERT INTO jobs (company_key, company_display, title, title_norm, location_kind, work_mode_possible, "
    "work_mode_source, employment_source, ad_language, completeness, posted_precision, first_seen_at, "
    "last_seen_at, availability, availability_changed_at, source_ids, field_provenance, det_facts_hash, "
    "merged_into_job_id, synthetic_eval) "
    "VALUES ('musterfirma', 'Musterfirma', 'Testrolle', 'testrolle', 'unknown', "
    "ARRAY['remote','hybrid','onsite'], 'none', 'none', 'de', 'full', 'unknown', now(), now(), 'active', "
    "now(), ARRAY['jsearch'], '{}', '\\x00', :merged_into, :synthetic) RETURNING id"
)
_REVISION: Final = q(
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status, embedding_id) "
    "VALUES (:job, 1, '\\x00', '{}', 'Erfundene Testanzeige.', 'full', 'de', :status, :embedding) "
    "RETURNING id"
)
_CURRENT_REVISION: Final = q("UPDATE jobs SET current_revision_id = :r WHERE id = :j")


async def insert_job(
    c: AsyncConnection,
    *,
    synthetic_eval: bool = False,
    merged_into_job_id: int | None = None,
    embedding_id: int | None = None,
) -> Job:
    """A job with one revision, its current one (embedded with `embedding_id` when given)."""
    params = {"merged_into": merged_into_job_id, "synthetic": synthetic_eval}
    job = int((await c.execute(_JOB, params)).scalar_one())
    status = "awaiting_embedding" if embedding_id is None else "embedded"
    revision_params = {"job": job, "status": status, "embedding": embedding_id}
    revision = int((await c.execute(_REVISION, revision_params)).scalar_one())
    await c.execute(_CURRENT_REVISION, {"r": revision, "j": job})
    return Job(job, revision)


_GROUP_MEMBER: Final = q("UPDATE jobs SET duplicate_group_id = :g WHERE id = :j")


async def insert_group(c: AsyncConnection, representative: Job, members: Sequence[Job]) -> int:
    """An active duplicate group of `representative` and `members` (`jobs.duplicate_group_id`)."""
    group = await insert_id(
        c,
        "duplicate_groups",
        {"representative_job_id": representative.job_id, "status": "active", "election_reason": "initial"},
    )
    for job in (representative, *members):
        await c.execute(_GROUP_MEMBER, {"g": group, "j": job.job_id})
    return group


async def insert_cycle(c: AsyncConnection, llm_tuple_id: int, *, status: str = "running") -> int:
    active = await active_ids(c)
    return await insert_id(
        c,
        "score_cycles",
        {
            "trigger": "manual",
            "llm_tuple_id": llm_tuple_id,
            "scope_version_id": active.scope_version_id,
            "evaluation_policy_id": active.evaluation_policy_id,
            "worker_epoch": 1,
            "status": status,
            "status_reason": "bug" if status == "failed" else None,
            "bucket_usd_at_start": Decimal(0),
            "c_hat_usd": Decimal("0.052646"),
            "admission_size": 0,
            "finished_at": None if status == "running" else NOW,
        },
    )


async def insert_bulk_operation(
    c: AsyncConnection,
    llm_tuple_id: int,
    *,
    state: str = "active",
    budget_usd: Decimal = Decimal(5),
    spent_usd: Decimal = Decimal(0),
) -> int:
    return await insert_id(
        c,
        "bulk_operations",
        {
            "kind": "tuple_rescore",
            "llm_tuple_id": llm_tuple_id,
            "state": state,
            "budget_usd": budget_usd,
            "spent_usd": spent_usd,
            "finished_at": None if state == "active" else NOW,
        },
    )


async def insert_target(c: AsyncConnection, job: Job, llm_tuple_id: int, **columns: object) -> int:
    """A `pending` target of `job`'s current revision unless `columns` say otherwise."""
    values = {
        "job_revision_id": job.revision_id,
        "llm_tuple_id": llm_tuple_id,
        "job_id": job.job_id,
        "state": "pending",
        **columns,
    }
    return await insert_id(c, "score_targets", values)


async def insert_attempt(c: AsyncConnection, target_id: int, **columns: object) -> int:
    """A `running` online attempt number 1 unless `columns` say otherwise; an ended one gets `ended_at`."""
    active = await active_ids(c)
    values: dict[str, object] = {
        "target_id": target_id,
        "attempt_no": 1,
        "mode": "online",
        "execution_config_id": active.execution_config_id,
        "evaluation_policy_id": active.evaluation_policy_id,
        "outcome": "running",
        **columns,
    }
    if values["outcome"] != "running":
        values.setdefault("ended_at", NOW)
    return await insert_id(c, "score_attempts", values)


@dataclass(frozen=True)
class Claimed:
    """An `in_progress` target and its `running` attempt: the owners a scoring call names, with the claim
    (`claim_id`, `attempt_no`) the target's ownership predicate tests."""

    target_id: int
    attempt_id: int
    claim_id: UUID
    attempt_no: int


async def insert_claimed(
    c: AsyncConnection, llm_tuple_id: int, *, attempt_no: int = 1, **target_columns: object
) -> Claimed:
    """A target of a new job for the tuple, claimed (epoch 1, attempt `attempt_no`), with its `running` online
    attempt under the same claim, both in lane `similarity` unless `target_columns` names another;
    `target_columns` add to the target's columns (a `bulk_operation_id`)."""
    claim = uuid4()
    lane = target_columns.pop("lane", "similarity")
    target = await insert_target(
        c,
        await insert_job(c),
        llm_tuple_id,
        state="in_progress",
        claim_id=claim,
        claim_epoch=1,
        claimed_at=NOW,
        claim_heartbeat_at=NOW,
        attempt_no=attempt_no,
        lane=lane,
        **target_columns,
    )
    attempt = await insert_attempt(c, target, attempt_no=attempt_no, claim_id=claim, claim_epoch=1, lane=lane)
    return Claimed(target, attempt, claim, attempt_no)


_BUDGET: Final = q(
    "INSERT INTO llm_budget (month, cap_usd, overrun_buffer_usd, bucket_usd, bucket_at) "
    "VALUES ('2026-10-01', 100, 2, 1, now()) ON CONFLICT DO NOTHING"
)


async def insert_scoring_call(
    c: AsyncConnection,
    *,
    attempt_id: int,
    target_id: int,
    transport: str = "stream",
    batch_item_id: int | None = None,
    bulk_operation_id: int | None = None,
    purpose: str = "generate",
) -> tuple[int, int]:
    """A `reserved` scoring ledger row (October 2026) and its call of the attempt and target, for `purpose`
    (`generate`, `revise` or `critic`).

    A streamed call is logical call 1; a batch call names its `batch_item_id`. Returns `(ledger_id, call_id)`.
    """
    await c.execute(_BUDGET)
    ledger = await insert_id(
        c,
        "llm_spend_ledger",
        {
            "budget_month": _BUDGET_MONTH,
            "kind": "request",
            "purpose": purpose,
            "purpose_class": "scoring",
            "model_requested": "claude-sonnet-5-5",
            "state": "reserved",
            "reserved_usd": Decimal("0.1"),
            "batch": transport == "batch",
            "bulk_operation_id": bulk_operation_id,
            "admitted_cap_usd": Decimal(100),
            "counted_before_usd": Decimal(0),
        },
    )
    active = await active_ids(c)
    call = await insert_id(
        c,
        "llm_calls",
        {
            "purpose": purpose,
            "attempt_id": attempt_id,
            "target_id": target_id,
            "round": 0,
            "logical_call_no": 1 if transport == "stream" else None,
            "execution_config_id": active.execution_config_id,
            "model_requested": "claude-sonnet-5-5",
            "effort": "medium",
            "max_tokens": 6000,
            "transport": transport,
            "batch_item_id": batch_item_id,
            "prompt_template_version": "v2",
            "rendered_prompt_sha256": b"\x00",
            "input_refs": {},
            "input_tokens_bound": 1000,
            "status": "reserved",
            "spend_ledger_id": ledger,
        },
    )
    return ledger, call


async def insert_draft(c: AsyncConnection, attempt_id: int, llm_call_id: int, round_: int = 0) -> int:
    values = {
        "attempt_id": attempt_id,
        "round": round_,
        "llm_call_id": llm_call_id,
        "output_json": {},
        "validation": {},
        "provisional": {},
    }
    return await insert_id(c, "score_drafts", values)


@dataclass(frozen=True)
class Published:
    """A published target and the rows its result stands on."""

    target_id: int
    attempt_id: int
    call_id: int
    draft_id: int
    result_id: int


_NEXT_RESULT_ID: Final = q("SELECT nextval(pg_get_serial_sequence('llm_results', 'id'))")
_TUPLE: Final = q(
    "SELECT profile_version_id, rubric_version_id, prompt_schema_version_id, model_policy_id "
    "FROM llm_tuples WHERE id = :t"
)


async def result_values(
    c: AsyncConnection, *, target_id: int, attempt_id: int, draft_id: int, job: Job, llm_tuple_id: int
) -> dict[str, object]:
    """A valid `accepted` `llm_results` row of the target (critic not run), without an `id`."""
    llm_tuple = (await c.execute(_TUPLE, {"t": llm_tuple_id})).one()
    active = await active_ids(c)
    return {
        "target_id": target_id,
        "attempt_id": attempt_id,
        "final_draft_id": draft_id,
        "job_revision_id": job.revision_id,
        "profile_version_id": llm_tuple.profile_version_id,
        "rubric_version_id": llm_tuple.rubric_version_id,
        "prompt_schema_version_id": llm_tuple.prompt_schema_version_id,
        "model_policy_id": llm_tuple.model_policy_id,
        "evaluation_policy_id": active.evaluation_policy_id,
        "status": "accepted",
        "criteria": {},
        "job_facts": {},
        "knockout_candidates": {},
        "fact_confirmations": {},
        "not_a_job_ad": False,
        "injection_suspected": False,
        "critic_outcome": "not_run",
        "critic_injection": None,
        "revisions_used": 0,
        "summary_en": "Invented summary.",
        "summary_de": "Erfundene Zusammenfassung.",
    }


async def insert_published(
    c: AsyncConnection,
    job: Job,
    llm_tuple_id: int,
    *,
    bulk_operation_id: int | None = None,
    cycle_id: int | None = None,
) -> Published:
    """A `published_accepted` target of `job` for the tuple, with its attempt, call, draft and result."""
    result = int((await c.execute(_NEXT_RESULT_ID)).scalar_one())
    target = await insert_target(
        c,
        job,
        llm_tuple_id,
        state="published_accepted",
        published_result_id=result,
        bulk_operation_id=bulk_operation_id,
    )
    attempt = await insert_attempt(
        c, target, outcome="published_accepted", cycle_id=cycle_id, lane="similarity"
    )
    _, call = await insert_scoring_call(
        c, attempt_id=attempt, target_id=target, bulk_operation_id=bulk_operation_id
    )
    draft = await insert_draft(c, attempt, call)
    values = await result_values(
        c, target_id=target, attempt_id=attempt, draft_id=draft, job=job, llm_tuple_id=llm_tuple_id
    )
    await insert_row(c, "llm_results", {"id": result, **values}, overriding=True)
    return Published(target, attempt, call, draft, result)


async def insert_det_result(
    c: AsyncConnection,
    llm_result_id: int,
    job_id: int,
    scope_version_id: int,
    *,
    low: Decimal,
    high: Decimal,
    current: bool = True,
) -> int:
    """A score instance with `low`, `high` and their rounded mid; a superseded one when not `current`."""
    mid = ((low + high) / 2).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return await insert_id(
        c,
        "det_results",
        {
            "llm_result_id": llm_result_id,
            "scope_version_id": scope_version_id,
            "job_id": job_id,
            "det_algorithm_version": "det-test",
            "is_current": current,
            "superseded_at": None if current else NOW,
            "det_criteria": {},
            "knockouts": {},
            "industry_match": "neutral",
            "caps_applied": {},
            "score_low": low,
            "score_mid": mid,
            "score_high": high,
            "coverage": Decimal("0.9"),
            "confidence": "high",
            "det_inputs": {},
            "det_inputs_hash": b"\x00",
        },
    )


async def insert_submission(
    c: AsyncConnection, llm_tuple_id: int, bulk_operation_id: int, **columns: object
) -> int:
    """A `preparing` batch submission of one request unless `columns` say otherwise."""
    values = {
        "state": "preparing",
        "llm_tuple_id": llm_tuple_id,
        "bulk_operation_id": bulk_operation_id,
        "request_count": 1,
        "request_hash": b"\x00",
        "reserved_usd": Decimal(0),
        "followup_held_usd": Decimal(0),
        **columns,
    }
    return await insert_id(c, "batch_submissions", values)


async def insert_batch_item(
    c: AsyncConnection, submission_id: int, target_id: int, attempt_no: int = 1, **columns: object
) -> int:
    """An `awaiting` item of the submission with the custom id `t<target>_a<attempt>`."""
    values = {
        "submission_id": submission_id,
        "target_id": target_id,
        "attempt_no": attempt_no,
        "custom_id": f"t{target_id}_a{attempt_no}",
        **columns,
    }
    return await insert_id(c, "batch_items", values)


async def insert_golden_case(c: AsyncConnection, job: Job, profile_version_id: int, **columns: object) -> int:
    """An active `week1` golden case labelled `good` under the active scope unless `columns` say otherwise."""
    active = await active_ids(c)
    values = {
        "job_revision_id": job.revision_id,
        "profile_version_id": profile_version_id,
        "scope_version_id": active.scope_version_id,
        "label": "good",
        "source": "week1",
        "det_inputs_snapshot": {},
        "det_input_sources": ["jsearch"],
        **columns,
    }
    return await insert_id(c, "golden_cases", values)


_M1_CONFIGURATION: Final = (
    "INSERT INTO scope_versions (scope_hash, payload) VALUES ('\\x01', '{}')",
    "INSERT INTO model_policies (policy_hash, payload) VALUES ('\\x01', '{}')",
    "INSERT INTO execution_configs (config_hash, payload) VALUES ('\\x01', '{}')",
    "INSERT INTO evaluation_policies (version_no, policy_hash, payload, projected_critic_rate) "
    "VALUES (1, '\\x01', '{}', 0.3)",
    "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, effect_classes) "
    "SELECT 1, %(schema_version)s, %(payload)s, s.id, m.id, e.id, p.id, '{none}' "
    "FROM scope_versions s, model_policies m, execution_configs e, evaluation_policies p",
    # M1's `active_config`: the four pointers of settings version 1, the rubric and prompt-schema ones NULL
    "INSERT INTO active_config (id, settings_version_id, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id) SELECT 1, id, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id FROM settings_versions WHERE version_no = 1",
)


def insert_m1_configuration(server: PgServer) -> None:
    """The configuration an M1 `migrate` leaves, on an empty database at 0006 or 0007: settings version 1 (the
    packaged payload, whose schedule the next seed reads) over one row of each derived table, and the
    `active_config` row naming them, with NULL rubric and prompt-schema pointers and no rows behind them."""
    params = {"schema_version": SETTINGS_SCHEMA_VERSION, "payload": Jsonb(load_settings_v1())}
    with psycopg.connect(server.dsn("migrator")) as conn:
        for statement in _M1_CONFIGURATION:
            conn.execute(statement, params if "%(" in statement else None)


def insert_m2_seed_rows(server: PgServer, *, point: bool) -> tuple[int, int]:
    """Rubric v1 and prompt-schema version 1 as M2's `migrate` seeded them, on a database at 0007; with
    `point`, `active_config` names them as that seed left it. Returns `(rubric_id, prompt_schema_id)`."""
    rubric = load_rubric(RUBRIC_V1)
    templates, schemas = prompt_schema_v1()
    with psycopg.connect(server.dsn("migrator")) as conn:
        rubric_row = conn.execute(
            "INSERT INTO rubric_versions (version, content_hash, weights, anchors) VALUES (%s, %s, %s, %s) "
            "RETURNING id",
            (RUBRIC_V1, rubric.content_hash, Jsonb(dict(rubric.weights)), Jsonb(dict(rubric.anchors))),
        ).fetchone()
        prompt_row = conn.execute(
            "INSERT INTO prompt_schema_versions (prompt_schema_hash, templates, schemas) VALUES (%s, %s, %s) "
            "RETURNING id",
            (prompt_schema_hash(templates, schemas), Jsonb(templates), Jsonb(schemas)),
        ).fetchone()
        if rubric_row is None or prompt_row is None:
            raise LookupError("an INSERT ... RETURNING returned no row")
        if point:
            conn.execute(
                "UPDATE active_config SET rubric_version_id = %s, prompt_schema_version_id = %s",
                (rubric_row[0], prompt_row[0]),
            )
    return int(rubric_row[0]), int(prompt_row[0])
