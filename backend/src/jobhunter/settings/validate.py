"""Settings validation (spec §13.1, §6.9.1, §10.3 G5, §11.13): `validate_fields` answers a proposed payload
with `FieldIssue`s, and `warnings_only` gives the field-level warnings the dry-run joins (plan Task 15,
pre-flight F16).

**Paths** start at `payload` and follow the document: `.key` into an object, `[i]` into a list, `.lang` into
a map (`payload.scope.locations[0].radius_km`, `payload.scope.language_policy.fr`). `field_path` builds them.

**Codes:**
- `required`: a key without a default is missing (a location's `radius_km`, a node's `effort`);
- `out_of_range`: a number outside its §13.1 range, a list or string longer or shorter than allowed, and an
  overrun buffer below its §6.9.1 bound (a lower limit that the other fields set);
- `unsupported`: a model or effort that §10.3 does not list, or that the node's model does not support (G5);
- `conflict`: a rule across fields (below), and selecting generator effort `low` while the eval gate has
  failed;
- `invalid`: anything else, an unknown key, a wrong JSON type or value, a repeated set value included, and a
  string holding a NUL character (U+0000), which no JSONB column can store, or a lone UTF-16 surrogate (a JSON
  escape such as `"\\ud800"`), which no UTF-8 text holds and which `effects` and the hashes would fail to
  encode (`types.unstorable`), at any depth of the document.

**Order.** The Pydantic errors come first, then an unstorable-string issue at each string path the Pydantic
errors do not already name; when there is any Pydantic error, the payload has no `Settings` and no other rule
runs. Otherwise the unstorable-string issues come first, then every rule runs, in document order, and every
issue it finds is reported:
1. scope: at least one location or `remote_within_germany`; no place twice (`gazetteer_id`), a label that
   has a location key (`run_settings.slug`, the `coverage_cells.location_key`), no key twice and none equal
   to `sources.base.REMOTE_DE`, the key of the remote-within-Germany cells; no industry and no company twice
   once normalised (the model stores them case-folded and as company keys), and `industries_include ∩
   industries_exclude = ∅`, at each excluded entry that is also included; `seniority_range.min ≤ max` on the
   ladder when both are set;
2. `evaluation.t3_min_mid > relevance_threshold`;
3. not every source disabled (SerpApi alone is the warning `serpapi_disabled_no_fallback`);
4. no `query_id` and no `watchlist_company_id` twice (the run view keys them, `RunSettings.queries`);
5. each node's effort, and the critic's light effort, among `pricing.SUPPORTED_EFFORTS` of its model; the
   generator's effort `low` needs a passing eval gate (§11.13, L3827): selecting it (the `active` settings'
   generator effort is not `low`, or no active settings are given) is an error while the gate has failed,
   and a document that carries the active `low` unchanged is valid with the warning `eval_failed` (Ruling
   T20-d); either way the warning `eval_uncalibrated` while the gate is uncalibrated (`warnings_only`);
6. each node's `max_tokens_retry ≥ max_tokens`;
7. `llm.overrun_buffer_usd ≥ overrun_buffer_bound(settings)`.

`validate_fields` returns a `Settings` only for a payload with no issue at all, so a caller cannot act on a
refused document.

**`eval_gate_state(conn, candidate=…)`** (M3 Task 20) is the eval gate of a candidate configuration (a
`Candidate`, by default the active one): `uncalibrated` below 30 of the user's active golden cases (the
adversarial ones never count, `gate_cases`), else the newest
completed golden production run of the candidate with live provenance decides, and no run is `failed`. The
dry-run and the save pass the proposed payload's candidate (`derive.candidate_of`).

**`schema_verified`** (M3 Task 11, Ruling P11) says whether a prompt-schema version's live schema dry-runs
passed every (schema, model) pair a model policy needs; the score lane's pause guard reads it, and so does the
save's refusal of an unverified model policy (`save._check_eval_gate`, M3 Task 20), whose title names the
pairs without a pass (`unverified_pairs`, the same reads).
"""

import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Final, Literal, cast

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.llm import bounds, pricing, schema_dry_run
from jobhunter.llm.pricing import Model
from jobhunter.normalize.titles import SENIORITY_LADDER
from jobhunter.quota import dryrun
from jobhunter.run_settings import slug
from jobhunter.settings.model import ModelPolicy, Settings
from jobhunter.sources.base import REMOTE_DE
from jobhunter.types import JSONValue, unstorable

GateState = Literal["uncalibrated", "passed", "failed"]
IssueCode = Literal["out_of_range", "invalid", "required", "conflict", "unsupported"]

ROOT: Final = "payload"
# §6.9.1's `max_concurrent_llm_calls`: 3 score calls and 2 auxiliary calls online, plus `batch_max_items`
# batch items at batch prices.
ONLINE_SCORE_SLOTS: Final = 3
ONLINE_AUX_SLOTS: Final = 2
_TOKENS_PER_MTOK: Final = 1_000_000
_CENT: Final = Decimal("0.01")
_CODES: Final[frozenset[str]] = frozenset({"out_of_range", "invalid", "required", "conflict", "unsupported"})
_RANGE_ERRORS: Final = frozenset(
    {
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "too_short",
        "too_long",
        "string_too_short",
        "string_too_long",
    }
)
_LEVEL: Final = {name: level for level, name in enumerate(SENIORITY_LADDER)}


@dataclass(frozen=True)
class FieldIssue:
    """A refusal of one field: its path, a code and a message that never repeats the value."""

    field: str
    code: IssueCode
    message: str


def field_path(loc: Sequence[str | int]) -> str:
    """The issue path of a location in the document (module docstring); Pydantic's `[key]` marker of a map key
    is dropped, so a refused key and a refused value of a map share the key's path."""
    path = ROOT
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif part != "[key]":
            path += f".{part}"
    return path


def _code(error_type: str) -> IssueCode:
    if error_type == "missing":
        return "required"
    if error_type in _RANGE_ERRORS:
        return "out_of_range"
    if error_type in _CODES:  # the model's own error types
        return cast(IssueCode, error_type)
    return "invalid"


def _issue(code: IssueCode, message: str, *loc: str | int) -> FieldIssue:
    return FieldIssue(field_path(loc), code, message)


def _unstorable_strings(value: JSONValue, loc: tuple[str | int, ...] = ()) -> Iterator[FieldIssue]:
    """An `invalid` issue at each string of `value` that holds a NUL character or a lone surrogate
    (`types.unstorable`), in document order."""
    if isinstance(value, str):
        if (message := unstorable(value)) is not None:
            yield FieldIssue(field_path(loc), "invalid", message)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _unstorable_strings(item, (*loc, key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _unstorable_strings(item, (*loc, i))


def validate_fields(
    payload: JSONValue, *, gate: GateState = "uncalibrated", active: Settings | None = None
) -> tuple[Settings | None, list[FieldIssue]]:
    """The settings of `payload` and no issue, or no settings and every issue (module docstring); `active` is
    the settings the document would replace (rule 5 tells a selection of generator effort `low` from a kept
    one by it)."""
    unsafe = list(_unstorable_strings(payload))
    try:
        settings = Settings.model_validate(payload)
    except ValidationError as exc:
        errors = [
            FieldIssue(field_path(error["loc"]), _code(error["type"]), error["msg"])
            for error in exc.errors(include_url=False, include_input=False)
        ]
        named = {error.field for error in errors}
        return None, [*errors, *(issue for issue in unsafe if issue.field not in named)]
    issues = [
        *unsafe,
        *_scope(settings),
        *_evaluation(settings),
        *_sources(settings),
        *_entries(settings),
        *_model_policy(settings, gate, active),
        *_execution(settings),
        *_overrun_buffer(settings),
    ]
    return (None, issues) if issues else (settings, [])


def _scope(settings: Settings) -> Iterator[FieldIssue]:
    scope = settings.scope
    if not scope.locations and not scope.remote_within_germany:
        yield _issue(
            "conflict", "at least one location is needed unless remote_within_germany", "scope", "locations"
        )
    places: set[str] = set()
    keys: set[str] = set()
    for i, location in enumerate(scope.locations):
        if location.gazetteer_id in places:
            yield _issue("conflict", "the same place appears twice", "scope", "locations", i, "gazetteer_id")
        places.add(location.gazetteer_id)
        try:
            key = slug(location.label)
        except ValueError:
            yield _issue("invalid", "a label needs a letter or a digit", "scope", "locations", i, "label")
            continue
        if key == REMOTE_DE:
            yield _issue(
                "conflict",
                f"the location key {REMOTE_DE} is reserved for remote work within Germany",
                *("scope", "locations", i, "label"),
            )
        elif key in keys:
            yield _issue(
                "conflict", "two locations have the same location key", "scope", "locations", i, "label"
            )
        keys.add(key)
    # industries are stored case-folded and companies as company keys (settings.model): compared as stored
    yield from _repeats(scope.industries_include, "the same industry appears twice", "industries_include")
    included = set(scope.industries_include)
    excluded: set[str] = set()
    for j, industry in enumerate(scope.industries_exclude):  # one issue per entry: a repeat first
        if industry in excluded:
            yield _issue("conflict", "the same industry appears twice", "scope", "industries_exclude", j)
        elif industry in included:
            yield _issue("conflict", "also in industries_include", "scope", "industries_exclude", j)
        excluded.add(industry)
    yield from _repeats(scope.excluded_companies, "the same company appears twice", "excluded_companies")
    seniority = scope.seniority_range
    if (
        seniority.min is not None
        and seniority.max is not None
        and _LEVEL[seniority.min] > _LEVEL[seniority.max]
    ):
        yield _issue("conflict", "min is above max", "scope", "seniority_range", "min")


def _repeats(values: list[str], message: str, field: str) -> Iterator[FieldIssue]:
    """A `conflict` at each scope list entry whose normal form an earlier entry already has."""
    seen: set[str] = set()
    for i, value in enumerate(values):
        if value in seen:
            yield _issue("conflict", message, "scope", field, i)
        seen.add(value)


def _evaluation(settings: Settings) -> Iterator[FieldIssue]:
    if settings.evaluation.t3_min_mid <= settings.evaluation.relevance_threshold:
        yield _issue("conflict", "must be above relevance_threshold", "evaluation", "t3_min_mid")


def _sources(settings: Settings) -> Iterator[FieldIssue]:
    if not any(settings.sources.enabled.model_dump().values()):
        yield _issue("conflict", "at least one source must be enabled", "sources", "enabled")


def _entries(settings: Settings) -> Iterator[FieldIssue]:
    queries: set[int] = set()
    for i, query in enumerate(settings.queries):
        if query.query_id in queries:
            yield _issue("conflict", "the same query appears twice", "queries", i, "query_id")
        queries.add(query.query_id)
    companies: set[int] = set()
    for i, company in enumerate(settings.watchlist):
        if company.watchlist_company_id in companies:
            yield _issue("conflict", "the same company appears twice", "watchlist", i, "watchlist_company_id")
        companies.add(company.watchlist_company_id)


def _selects_generator_low(settings: Settings, active: Settings | None) -> bool:
    """Whether `settings` changes the generator's effort to `low` (Ruling T20-d): it is `low` and the active
    settings' is not, or no active settings are known."""
    selected = settings.scoring.model_policy.nodes.generate.effort == "low"
    kept = active is not None and active.scoring.model_policy.nodes.generate.effort == "low"
    return selected and not kept


def _model_policy(settings: Settings, gate: GateState, active: Settings | None) -> Iterator[FieldIssue]:
    policy = settings.scoring.model_policy
    where = ("scoring", "model_policy")
    nodes = policy.nodes.by_node()
    for node, choice in nodes.items():
        if choice.effort not in pricing.SUPPORTED_EFFORTS[choice.model]:
            yield _issue(
                "unsupported", f"{choice.model} does not support this effort", *where, "nodes", node, "effort"
            )
    critic = nodes["critic"].model
    if policy.critic_light_effort not in pricing.SUPPORTED_EFFORTS[critic]:
        yield _issue("unsupported", f"{critic} does not support this effort", *where, "critic_light_effort")
    if gate == "failed" and _selects_generator_low(settings, active):
        yield _issue(
            "conflict",
            "selecting generator effort low needs a passing eval (§11.13)",
            *where,
            "nodes",
            "generate",
            "effort",
        )


def _execution(settings: Settings) -> Iterator[FieldIssue]:
    for node, stage in settings.scoring.execution.by_node().items():
        if stage.max_tokens_retry < stage.max_tokens:
            yield _issue(
                "conflict", "must be at least max_tokens", "scoring", "execution", node, "max_tokens_retry"
            )


def _overrun_buffer(settings: Settings) -> Iterator[FieldIssue]:
    bound = overrun_buffer_bound(settings)
    if settings.llm.overrun_buffer_usd < bound:
        yield _issue(
            "out_of_range",
            f"must be at least {bound}, the §6.9.1 bound of the concurrent calls and batch_max_items",
            "llm",
            "overrun_buffer_usd",
        )


def overrun_buffer_bound(settings: Settings, *, overhead: Mapping[Model, int] | None = None) -> Decimal:
    """§6.9.1: `max_concurrent_llm_calls x PROMPT_OVERHEAD_TOKENS x the highest input price per token`, summed
    per slot and rounded up to the cent.

    The 3 score and 2 auxiliary online slots each take the costliest model of the policy
    (`PROMPT_OVERHEAD_TOKENS[m] x bound_input_price(m, batch=False)`); the `batch_max_items` batch slots take
    the generator's model at batch prices, because batch items are generator items (§10.10). `overhead`
    replaces `bounds.PROMPT_OVERHEAD_TOKENS` (the V19 spike's analysis passes the constants it would write).
    """
    tokens = bounds.PROMPT_OVERHEAD_TOKENS if overhead is None else overhead
    nodes = settings.scoring.model_policy.nodes.by_node()
    online = max(tokens[c.model] * pricing.bound_input_price(c.model, batch=False) for c in nodes.values())
    generator = nodes["generate"].model
    batch = (
        settings.scoring.batch_max_items
        * tokens[generator]
        * pricing.bound_input_price(generator, batch=True)
    )
    total = (ONLINE_SCORE_SLOTS + ONLINE_AUX_SLOTS) * online + batch
    return (total / _TOKENS_PER_MTOK).quantize(_CENT, rounding=ROUND_CEILING)


def warnings_only(
    settings: Settings, *, gate: GateState, active: Settings | None = None
) -> list[dryrun.Warning]:
    """The field-level warnings of valid `settings`, for the dry-run (§6.8) to join after a clean
    `validate_fields` with the same `gate` and `active`: for generator effort `low`, `eval_uncalibrated`
    while the gate is uncalibrated (§11.13) and `eval_failed` while it has failed (a kept `low`: selecting it
    then is an error, Ruling T20-d); and `serpapi_disabled_no_fallback` when SerpApi is disabled (§13.1)."""
    warnings: list[dryrun.Warning] = []
    low = settings.scoring.model_policy.nodes.generate.effort == "low"
    if low and gate == "uncalibrated":
        warnings.append(dryrun.Warning("eval_uncalibrated", node="generate"))
    elif low and gate == "failed" and not _selects_generator_low(settings, active):
        warnings.append(dryrun.Warning("eval_failed", node="generate"))
    if not settings.sources.enabled.serpapi:
        warnings.append(dryrun.Warning("serpapi_disabled_no_fallback"))
    return warnings


# ---- the eval gate (§11.13, M3 Task 20, B R14, Deviation 29) ---------------------------------------------

GATE_MIN_CASES: Final = 30  # L4631: before 30 active golden cases the gate is uncalibrated
FAKE_LLM_VAR: Final = "JOBHUNTER_FAKE_LLM"  # the process's fake LLM mode, as `config.load_config` reads it


@dataclass(frozen=True)
class Candidate:
    """The identity an eval run gives the gate (L4631): the rubric and prompt-schema versions and the model
    and evaluation policies by content hash, so a payload whose rows do not exist yet has one too
    (`derive.candidate_of`); the execution config is not part of it."""

    rubric_version_id: int
    prompt_schema_version_id: int
    model_policy_hash: bytes
    evaluation_policy_hash: bytes


def fake_llm_process() -> bool:
    """Whether this process runs on the fake LLM: `JOBHUNTER_FAKE_LLM` set to anything, as
    `config.load_config` reads it (the dev stack's fake overlay, the tests; prod refuses it at startup)."""
    return FAKE_LLM_VAR in os.environ


def gate_transports() -> tuple[str, ...]:
    """The transports of the runs the gate accepts as evidence (Codex r2 B20): live runs, and fake ones only
    while this process itself runs on the fake LLM (`fake_llm_process`)."""
    return ("live", "fake") if fake_llm_process() else ("live",)


# The cases the gate's 30 counts (L4608, L4630, Ruling T20-b): the user's active ones (week 1 and feedback);
# the adversarial cases are scored and pooled in the metrics but never calibrate the gate.
_GATE_CASES: Final = q("SELECT count(*) FROM golden_cases WHERE active AND source <> 'adversarial'")
_ACTIVE_CANDIDATE: Final = q(
    "SELECT a.rubric_version_id, a.prompt_schema_version_id, m.policy_hash AS model_policy_hash, "
    "e.policy_hash AS evaluation_policy_hash FROM active_config a "
    "JOIN model_policies m ON m.id = a.model_policy_id "
    "JOIN evaluation_policies e ON e.id = a.evaluation_policy_id WHERE a.id = 1"
)
# The newest ended golden run of the candidate on the production route with an accepted transport.
_GATE_RUN: Final = q(
    "SELECT r.passed FROM eval_runs r JOIN model_policies m ON m.id = r.model_policy_id "
    "JOIN evaluation_policies e ON e.id = r.evaluation_policy_id "
    "WHERE r.kind = 'golden' AND r.route = 'production' AND r.status = 'completed' "
    "AND r.rubric_version_id = :rubric AND r.prompt_schema_version_id = :prompt "
    "AND m.policy_hash = :model_policy AND e.policy_hash = :evaluation_policy "
    "AND r.metrics ->> 'transport' = ANY(CAST(:transports AS text[])) "
    "ORDER BY r.finished_at DESC, r.id DESC LIMIT 1"
)


async def gate_cases(conn: AsyncConnection) -> int:
    """The number of the user's active golden cases, which the gate's `GATE_MIN_CASES` counts (Ruling T20-b):
    the adversarial ones are left out. `eval_gate_state` and an eval run's own gate both read it."""
    return int((await conn.execute(_GATE_CASES)).scalar_one())


async def eval_gate_state(conn: AsyncConnection, *, candidate: Candidate | None = None) -> GateState:
    """The eval gate of `candidate` (§11.13; the active configuration's when `None`), plain reads:
    `uncalibrated` while fewer than `GATE_MIN_CASES` of the user's golden cases are active (`gate_cases`: the
    adversarial ones never count); otherwise the newest `completed`
    golden run of the candidate on the production route whose transport is one `gate_transports` accepts
    decides: `passed` when it passed, `failed` when it failed or was never gated (`passed` NULL), and `failed`
    when there is none (user decision "Eval gate = fail until a run": a run is required)."""
    if await gate_cases(conn) < GATE_MIN_CASES:
        return "uncalibrated"
    if candidate is None:
        row = (await conn.execute(_ACTIVE_CANDIDATE)).one()
        candidate = Candidate(
            int(row.rubric_version_id),
            int(row.prompt_schema_version_id),
            bytes(row.model_policy_hash),
            bytes(row.evaluation_policy_hash),
        )
    passed = (
        await conn.execute(
            _GATE_RUN,
            {
                "rubric": candidate.rubric_version_id,
                "prompt": candidate.prompt_schema_version_id,
                "model_policy": candidate.model_policy_hash,
                "evaluation_policy": candidate.evaluation_policy_hash,
                "transports": list(gate_transports()),
            },
        )
    ).scalar_one_or_none()
    return "passed" if passed is True else "failed"


# ---- the live schema dry-run (M3 Task 11, Ruling P11) ----------------------------------------------------

_MODEL_POLICY: Final = q("SELECT payload FROM model_policies WHERE id = :id")
# The (schema, model) pairs of every passed case of the live dry-runs of a version, whatever the run's end.
_PASSED_LIVE: Final = q(
    "SELECT DISTINCT c ->> 'schema' AS schema, c ->> 'model' AS model FROM eval_runs r "
    "CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(r.metrics -> 'cases') = 'array' "
    "THEN r.metrics -> 'cases' ELSE '[]'::jsonb END) AS c "
    "WHERE r.kind = 'schema_dry_run' AND r.prompt_schema_version_id = :version "
    "AND r.metrics ->> 'transport' = 'live' AND c -> 'passed' = 'true'::jsonb"
)


def schema_pairs(model_policy: ModelPolicy) -> frozenset[tuple[str, str]]:
    """The (schema, model) pairs `model_policy`'s nodes need, by the dry-run's own mapping of each schema to
    its nodes (`schema_dry_run.SCHEMA_NODES`): the generator schema on the `generate` and `revise` models,
    the critic schema on the `critic` model, the profile schema on the `extract_profile` model and the
    queries schema on the `propose_queries` model."""
    models: dict[str, str] = {node: choice.model for node, choice in model_policy.nodes.by_node().items()}
    return frozenset(
        (schema, models[node]) for schema, nodes in schema_dry_run.SCHEMA_NODES for node in nodes
    )


async def unverified_pairs(
    conn: AsyncConnection, *, prompt_schema_version_id: int, model_policy: ModelPolicy
) -> list[tuple[str, str]]:
    """The (schema, model) pairs `model_policy` needs (`schema_pairs`) that no live schema dry-run of the
    prompt-schema version passed, sorted: what a refusal names when `schema_verified` is false (M3 Task 20).
    The same reads as `schema_verified`'s, which stays the one verdict."""
    rows = await conn.execute(_PASSED_LIVE, {"version": prompt_schema_version_id})
    passed = {(str(row.schema), str(row.model)) for row in rows}
    return sorted(schema_pairs(model_policy) - passed)


async def schema_verified(
    conn: AsyncConnection, *, prompt_schema_version_id: int, model_policy_id: int
) -> bool:
    """Whether the live schema dry-runs of the prompt-schema version hold a passed case for every (schema,
    model) pair the model policy's nodes need (`schema_pairs`; Codex r1 #25, r2 B24, A3 G30, Deviation 5).

    Over every `schema_dry_run` run of the version with `metrics.transport = 'live'` (a fake run never
    counts), whatever its end and whichever policy it ran for: a new policy that reuses verified models needs
    no new run, and one that moves a node to a model without a passed live case is refused. `LookupError` for
    a model policy that does not exist. The pause guard of the score lane reads it (`scoring.cycles`); Task 20
    adds the save-time refusal and Task 21 the prompt-schema activation."""
    payload = (await conn.execute(_MODEL_POLICY, {"id": model_policy_id})).scalar_one_or_none()
    if payload is None:
        raise LookupError(f"model policy {model_policy_id} does not exist")
    needed = schema_pairs(ModelPolicy.model_validate(payload))
    rows = await conn.execute(_PASSED_LIVE, {"version": prompt_schema_version_id})
    passed = {(str(row.schema), str(row.model)) for row in rows}
    return needed <= passed
