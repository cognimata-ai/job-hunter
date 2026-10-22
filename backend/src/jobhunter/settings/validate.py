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
- `conflict`: a rule across fields (below), and generator effort `low` while the eval gate has failed;
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
   generator's effort `low` needs a passing eval gate (§11.13): an error when the gate failed, the warning
   `eval_uncalibrated` while it is uncalibrated (`warnings_only`);
6. each node's `max_tokens_retry ≥ max_tokens`;
7. `llm.overrun_buffer_usd ≥ overrun_buffer_bound(settings)`.

`validate_fields` returns a `Settings` only for a payload with no issue at all, so a caller cannot act on a
refused document.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Final, Literal, cast

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.llm import bounds, pricing
from jobhunter.llm.pricing import Model
from jobhunter.normalize.titles import SENIORITY_LADDER
from jobhunter.quota import dryrun
from jobhunter.run_settings import slug
from jobhunter.settings.model import Settings
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
    payload: JSONValue, *, gate: GateState = "uncalibrated"
) -> tuple[Settings | None, list[FieldIssue]]:
    """The settings of `payload` and no issue, or no settings and every issue (module docstring)."""
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
        *_model_policy(settings, gate),
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


def _model_policy(settings: Settings, gate: GateState) -> Iterator[FieldIssue]:
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
    if nodes["generate"].effort == "low" and gate == "failed":
        yield _issue(
            "conflict",
            "generator effort low needs a passing eval (§11.13)",
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


def warnings_only(settings: Settings, *, gate: GateState) -> list[dryrun.Warning]:
    """The field-level warnings of valid `settings`, for the dry-run (§6.8) to join after a clean
    `validate_fields`: `eval_uncalibrated` for generator effort `low` while the gate is uncalibrated (§11.13),
    and `serpapi_disabled_no_fallback` when SerpApi is disabled (§13.1)."""
    warnings: list[dryrun.Warning] = []
    if settings.scoring.model_policy.nodes.generate.effort == "low" and gate == "uncalibrated":
        warnings.append(dryrun.Warning("eval_uncalibrated", node="generate"))
    if not settings.sources.enabled.serpapi:
        warnings.append(dryrun.Warning("serpapi_disabled_no_fallback"))
    return warnings


async def eval_gate_state(conn: AsyncConnection) -> GateState:
    """The eval gate (§11.13): `uncalibrated` while fewer than 30 golden cases exist.

    `golden_cases` is M3's table, so in M2 there are none by construction and the answer needs no query; M3
    counts the active cases on `conn` and runs the gate.
    """
    return "uncalibrated"
