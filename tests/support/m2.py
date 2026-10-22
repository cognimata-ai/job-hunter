"""Synthetic M2 rows for the schema, fence and reset tests: ids and hashes only, no CV text.

`insert_profile_chain` builds the rows an active LLM tuple stands on, the way production does: a CV file, a
`ready` draft, the profile version it confirms (the two foreign keys between draft and version are deferred
and satisfied by the confirm; its `profile_json` is `{}` unless a `profile` is given), and a tuple over that
version, a test rubric row, a test prompt-schema row and the seeded model policy. `reset_database` removes
every one of them again, as it does the rows of the other helpers here.

`insert_vector` gives each (profile version, kind) an embedding row of its own, so two versions can both hold
a `summary` vector (it once hashed the kind alone, and a second version's `summary` collided on that row).
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.types import JSONValue
from sqlalchemy.ext.asyncio import AsyncConnection

_CV_FILE = q("INSERT INTO cv_files (sha256, mime, size_bytes) VALUES (:h, 'text/plain', 1) RETURNING id")
_DRAFT = q(
    "INSERT INTO profile_drafts (cv_file_id, status, reviewed_text_sha256, draft_json) "
    "VALUES (:cv, 'ready', :h, '{}') RETURNING id"
)
_VERSION = q(
    "INSERT INTO profile_versions (version_no, draft_id, cv_file_id, profile_json, profile_hash) "
    "VALUES ((SELECT coalesce(max(version_no), 0) + 1 FROM profile_versions), :d, :cv, :profile, :h) "
    "RETURNING id",
    jsonb=["profile"],
)
_RUBRIC = q(
    "INSERT INTO rubric_versions (version, content_hash, weights, anchors) VALUES (:v, :h, '{}', '{}') "
    "RETURNING id"
)
_PROMPT_SCHEMA = q(
    "INSERT INTO prompt_schema_versions (prompt_schema_hash, templates, schemas) VALUES (:h, '{}', '{}') "
    "RETURNING id"
)
_TUPLE = q(
    "INSERT INTO llm_tuples (profile_version_id, rubric_version_id, prompt_schema_version_id, "
    "model_policy_id) SELECT :pv, :r, :ps, model_policy_id FROM active_config RETURNING id"
)
_EVAL_RUN = q(
    "INSERT INTO eval_runs (kind, route, status) "
    "VALUES ('schema_dry_run', 'production', :status) RETURNING id"
)
_BUDGET = q(
    "INSERT INTO llm_budget (month, cap_usd, overrun_buffer_usd, bucket_usd, bucket_at) "
    "VALUES ('2026-10-01', 100, 2, 1, now()) ON CONFLICT DO NOTHING"
)
_LEDGER = q(
    "INSERT INTO llm_spend_ledger (budget_month, kind, purpose, purpose_class, model_requested, state, "
    "reserved_usd, admitted_cap_usd, counted_before_usd) "
    "VALUES ('2026-10-01', 'request', :purpose, :cls, 'claude-opus-5-5', 'reserved', 1, 100, 0) "
    "RETURNING id"
)
_CALL = q(
    "INSERT INTO llm_calls (purpose, profile_draft_id, eval_run_id, execution_config_id, model_requested, "
    "effort, max_tokens, transport, prompt_template_version, rendered_prompt_sha256, input_refs, "
    "input_tokens_bound, status, spend_ledger_id) "
    "VALUES (:purpose, :draft, :eval_run, (SELECT execution_config_id FROM active_config), "
    "'claude-opus-5-5', 'medium', 16000, 'stream', 'v1', '\\x00', '{}', 1000, 'reserved', :ledger) "
    "RETURNING id"
)
_PROPOSAL = q(
    "INSERT INTO query_proposals (profile_version_id, status, items) "
    # JSONB binds None as JSON null, which NULLIF turns back into SQL NULL
    "VALUES (:pv, :status, NULLIF(:items, CAST('null' AS jsonb))) RETURNING id",
    jsonb=["items"],
)
_EMBEDDING = q(
    "INSERT INTO embeddings (embedding_model_id, text_sha256, input_kind, vector) "
    "VALUES ('m2-test', :h, 'query', CAST(:vector AS halfvec)) RETURNING id"
)
_VECTOR = q("INSERT INTO profile_vectors (profile_version_id, kind, embedding_id) VALUES (:pv, :kind, :e)")
_UNIT_VECTOR = "[" + ",".join(["0"] * 1023 + ["1"]) + "]"


@dataclass(frozen=True)
class ProfileChain:
    """The ids of one confirmed profile and the tuple over it."""

    cv_file_id: int
    draft_id: int
    profile_version_id: int
    rubric_version_id: int
    prompt_schema_version_id: int
    llm_tuple_id: int


def digest(n: int) -> bytes:
    """A distinct 8-byte test hash for number `n` (never a real digest)."""
    return b"m2-test-" + n.to_bytes(4, "big")


async def insert_cv_file(c: AsyncConnection, n: int = 1) -> int:
    return int((await c.execute(_CV_FILE, {"h": digest(n)})).scalar_one())


async def insert_profile_chain(
    c: AsyncConnection, n: int = 1, *, profile: Mapping[str, JSONValue] | None = None
) -> ProfileChain:
    """Insert chain number `n`; two chains of one transaction use two numbers. The version's `profile_json`
    is `profile` (`{}` when omitted)."""
    h = digest(n)
    cv = await insert_cv_file(c, n)
    draft = (await c.execute(_DRAFT, {"cv": cv, "h": h})).scalar_one()
    version = (
        await c.execute(_VERSION, {"d": draft, "cv": cv, "h": h, "profile": dict(profile or {})})
    ).scalar_one()
    await transition(
        c,
        "profile_drafts",
        draft,
        ["ready"],
        "confirmed",
        set_={"confirmed_profile_version_id": version, "confirm_response": {"profile_version_id": version}},
    )
    rubric = (await c.execute(_RUBRIC, {"v": f"test-{n}", "h": h})).scalar_one()
    prompt_schema = (await c.execute(_PROMPT_SCHEMA, {"h": h})).scalar_one()
    llm_tuple = (await c.execute(_TUPLE, {"pv": version, "r": rubric, "ps": prompt_schema})).scalar_one()
    return ProfileChain(cv, draft, version, rubric, prompt_schema, llm_tuple)


async def insert_eval_run(c: AsyncConnection, status: str = "running") -> int:
    return int((await c.execute(_EVAL_RUN, {"status": status})).scalar_one())


async def insert_llm_call(
    c: AsyncConnection,
    *,
    purpose: str = "extract_profile",
    draft_id: int | None = None,
    eval_run_id: int | None = None,
) -> tuple[int, int]:
    """A `reserved` non-scoring ledger row (and the October 2026 budget row, once) with its call.

    Returns `(ledger_id, call_id)`. A `purpose` of `generate`, `revise` or `critic` needs an `eval_run_id`
    (the owner CHECK of `llm_calls`) and is booked non-scoring, as an eval run's calls are.
    """
    await c.execute(_BUDGET)
    ledger = (await c.execute(_LEDGER, {"purpose": purpose, "cls": "nonscoring"})).scalar_one()
    call = (
        await c.execute(
            _CALL, {"purpose": purpose, "draft": draft_id, "eval_run": eval_run_id, "ledger": ledger}
        )
    ).scalar_one()
    return int(ledger), int(call)


async def insert_proposal(
    c: AsyncConnection, profile_version_id: int, status: str = "running", items: list[object] | None = None
) -> int:
    row = await c.execute(_PROPOSAL, {"pv": profile_version_id, "status": status, "items": items})
    return int(row.scalar_one())


async def insert_embedding(c: AsyncConnection, label: str) -> int:
    """An `embeddings` row (a query, the unit vector) whose text hash is that of `label`; nothing references
    it. Returns its id; the same `label` twice violates the table's unique key."""
    text_hash = hashlib.sha256(label.encode()).digest()
    return int((await c.execute(_EMBEDDING, {"h": text_hash, "vector": _UNIT_VECTOR})).scalar_one())


async def insert_vector(c: AsyncConnection, profile_version_id: int, kind: str = "summary") -> int:
    """A `profile_vectors` row over a fresh unit-vector embedding; returns the embedding id. The embedding's
    text hash comes from the version id and the kind, so every (version, kind) has a row of its own."""
    embedding = await insert_embedding(c, f"{profile_version_id}:{kind}")
    await c.execute(_VECTOR, {"pv": profile_version_id, "kind": kind, "e": embedding})
    return embedding
