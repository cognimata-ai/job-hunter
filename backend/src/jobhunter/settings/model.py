"""The settings document (spec §13.1): one typed Pydantic model of `settings_versions.payload`.

- **Groups.** Each §13.1 group is a sub-model with every field's type, range and default verbatim. The
  defaults live here and nowhere else: `Settings()` is the default document, and the packaged seed
  `settings_v1.json` (`db.seed.load_settings_v1`) is that document as JSON. `test_settings_model.py` pins the
  two byte-equal under `canonical_json`, and validating the seed gives it back through `to_payload()`. A key
  left out takes its default; `to_payload()` always writes the complete document.
- **Strict JSON types.** Unknown keys are refused at every level (`extra="forbid"`); a JSON `"6"` is no
  integer, `6.0` no integer either, and `1` no boolean. A coordinate may be a JSON integer.
- **Decimals** are JSON strings of plain decimal notation (`"2.00"`; no exponent, no float), read into
  `Decimal`; each value has one canonical form, written as the fixed-point string `format(value, "f")` (never
  an exponent, also for a `Decimal` built in Python), so `to_payload()` validates to itself, and `-0` is `0`:
  - the USD amounts (`cap_usd`, `overrun_buffer_usd`, `min_bucket_capacity_usd`) have a fixed scale of 2
    places, as `llm_budget` stores them (`numeric(10,2)`, §7.11): `"2"`, `"2.0"` and `"2.00"` write `"2.00"`;
  - the rates (`t2_straddle_max_coverage`, `bulk_rescore_max_budget_share`; §13.1 gives them no precision)
    take at most 2 decimal places and drop trailing zeros (Ruling R15-5): `"0.70"`, `"0.7"` and `"0.700"`
    write `"0.7"`, `"1.0"` writes `"1"`, `"0.65"` is kept and `"0.655"` is refused. The seed's `"0.7"` and
    `"0.4"` are already in that form.
  A value with more places than its field allows is refused (`invalid`).
- **Normal forms.** `industries_include`/`industries_exclude` are stored case-folded (`normalize.text.fold`;
  §13.1 "case-folded") and `excluded_companies` as company keys (`normalize.companies.company_key`, §8.3: what
  the hard filter compares a job's `company_key` with, §11.1), so a change of case or legal form is no
  change. Each normaliser is applied until the value no longer changes (`_settled`): both are idempotent on
  ordinary text, but not on a few inputs (`company_key` of a name ending in two country words, `fold` of a
  spacing diacritic), and a stored value must validate to itself.
- **Set-valued lists** (`work_modes`, `employment_types`, `accepted_ad_languages`, `critic_light_triggers`)
  hold no value twice and are kept in the order their type declares, so a reordering is no change and derives
  no new row (§13.2). Other lists keep their order.
- **Field checks only.** The rules across fields (§13.1's cross-field validation, the efforts a model
  supports, `max_tokens_retry ≥ max_tokens`, the §6.9.1 overrun-buffer bound, the eval gate) are
  `settings.validate.validate_fields`'s. So a stored version always loads (`run_settings.load_run_settings`),
  also one that a later rule refuses: a database seeded before spec revision 3.4 keeps `batch_max_items` 1,000
  in its version 1 (plan Deviation 34).
- **Error types.** A model or effort outside §10.3's lists raises the error type `unsupported` (G5); a
  decimal outside its range, or an industry outside 2 to 60 characters once folded, `out_of_range`; a
  malformed decimal or anchor, an unknown time zone, a repeated set value or a company name with no key
  `invalid`. `validate` maps these and Pydantic's own types to `FieldIssue` codes; it also refuses an
  industry or company that repeats once normalised (`conflict`).

`ScopeVersionPayload` is the scope without the admission field `max_ad_age_days`: the payload of a
`scope_versions` row (`db.seed.scope_payload`). `Scope` adds that field.
"""

import re
from collections.abc import Callable
from decimal import Decimal
from functools import cache
from typing import Annotated, Final, Literal, cast, get_args
from zoneinfo import available_timezones

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer
from pydantic_core import PydanticCustomError

from jobhunter.db.seed import ANCHOR_LOCAL
from jobhunter.llm.pricing import MODELS, Effort, Model
from jobhunter.normalize.companies import company_key
from jobhunter.normalize.text import fold
from jobhunter.types import JSONValue

Node = Literal["generate", "revise", "critic", "extract_profile", "propose_queries"]
NODES: Final[tuple[Node, ...]] = get_args(Node)
Severity = Literal["hard", "soft"]
WorkMode = Literal["remote", "hybrid", "onsite"]
EmploymentType = Literal[  # §11.5
    "full_time",
    "part_time",
    "contract",
    "temporary",
    "internship",
    "working_student",
    "apprenticeship",
    "freelance",
]
AdLanguage = Literal["de", "en", "other"]
LanguageCode = Literal["de", "en", "fr", "es", "it", "nl", "pl", "pt", "other"]  # §11.5 `lang_code`
# The §12.4 `seniority` enum in ladder order (`normalize.titles.SENIORITY_LADDER`; a test pins them equal).
SeniorityLevel = Literal[
    "intern", "working_student", "apprentice", "junior", "mid", "senior", "lead", "principal", "head"
]
LightTrigger = Literal["T2", "T3", "T8"]  # the triggers that may be disabled (§11.7.1)
_DEFAULT_AD_LANGUAGES: Final[tuple[AdLanguage, ...]] = ("de", "en")
_DEFAULT_LIGHT_TRIGGERS: Final[tuple[LightTrigger, ...]] = ("T2", "T8")

_EFFORTS: Final[tuple[str, ...]] = get_args(Effort)
_DECIMAL_TEXT: Final = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_USD_PLACES: Final = 2  # the fixed scale of the USD amounts (`llm_budget`: numeric(10,2))
_RATE_PLACES: Final = 2  # the most places of a rate, written without trailing zeros (Ruling R15-5)
_INDUSTRY_LENGTH: Final = (2, 60)
_SETTLE_ROUNDS: Final = 8
_BIGINT_MAX: Final = 2**63 - 1  # the ids are `bigint` row ids (`queries.id`, `watchlist_companies.id`)


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _decimal(value: object) -> object:
    """A JSON string of plain decimal notation, or a finite `Decimal`, as a `Decimal`; any other value is
    refused, a float too."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PydanticCustomError("invalid", "not a finite decimal number")
        return value
    if not isinstance(value, str):
        raise PydanticCustomError("invalid", 'a decimal is a JSON string, such as "2.00"')
    if _DECIMAL_TEXT.fullmatch(value) is None:
        raise PydanticCustomError(
            "invalid", 'a decimal is written as digits with an optional fraction, such as "2.00"'
        )
    return Decimal(value)


def _canonical(low: str, high: str, *, places: int, fixed: bool) -> AfterValidator:
    """The check of a decimal range `[low, high]` and of at most `places` decimal places, then the value's
    canonical form: at exactly `places` places when `fixed`, otherwise without trailing zeros (and never with
    a positive exponent, so `1.0` is `1`, not `1E+0`); a negative zero is zero."""
    lowest, highest = Decimal(low), Decimal(high)
    quantum = Decimal(1).scaleb(-places)

    def check(value: Decimal) -> Decimal:
        if not lowest <= value <= highest:
            raise PydanticCustomError(
                "out_of_range", "must be from {low} to {high}", {"low": low, "high": high}
            )
        scaled = value.quantize(quantum)
        if scaled != value:
            raise PydanticCustomError("invalid", "at most {places} decimal places", {"places": places})
        scaled += 0  # -0.00 becomes 0.00; the scale is kept
        if fixed:
            return scaled
        trimmed = scaled.normalize()
        exponent = trimmed.as_tuple().exponent
        return trimmed.quantize(Decimal(1)) if isinstance(exponent, int) and exponent > 0 else trimmed

    return AfterValidator(check)


def _usd(low: str, high: str) -> AfterValidator:
    return _canonical(low, high, places=_USD_PLACES, fixed=True)


def _rate(low: str, high: str) -> AfterValidator:
    return _canonical(low, high, places=_RATE_PLACES, fixed=False)


def _fixed_point(value: Decimal) -> str:
    return format(value, "f")


_DECIMAL: Final = BeforeValidator(_decimal)
_AS_STRING: Final = PlainSerializer(_fixed_point, return_type=str, when_used="json")


def _settled(normalize: Callable[[str], str], value: str) -> str:
    """`normalize` applied until the value no longer changes (module docstring, normal forms)."""
    for _ in range(_SETTLE_ROUNDS):
        again = normalize(value)
        if again == value:
            return value
        value = again
    raise PydanticCustomError("invalid", "has no stable normal form")


def _industry(value: str) -> str:
    """The case-folded industry, 2 to 60 characters long."""
    folded = _settled(fold, value)
    shortest, longest = _INDUSTRY_LENGTH
    if not shortest <= len(folded) <= longest:
        raise PydanticCustomError(
            "out_of_range",
            "must have from {shortest} to {longest} characters once case-folded",
            {"shortest": shortest, "longest": longest},
        )
    return folded


def _company(value: str) -> str:
    """The company key of a name or of a key (§8.3)."""
    key = _settled(company_key, value)
    if not key.strip():
        raise PydanticCustomError("invalid", "a company name needs a letter, a digit or a symbol")
    return key


def _set_of[T](declared: tuple[T, ...]) -> AfterValidator:
    """The check of a set-valued list: no value twice, then the values in their declared order."""

    def canonical(values: list[T]) -> list[T]:
        if len(set(values)) != len(values):
            raise PydanticCustomError("invalid", "a value appears more than once")
        return sorted(values, key=declared.index)

    return AfterValidator(canonical)


def _supported(kind: str, allowed: tuple[str, ...]) -> BeforeValidator:
    """A string outside `allowed` raises `unsupported` (§10.3 G5); a value of another type is left to the
    type's own check (`invalid`)."""

    def check(value: object) -> object:
        if isinstance(value, str) and value not in allowed:
            raise PydanticCustomError(
                "unsupported",
                "not a supported {kind} (one of {allowed})",
                {"kind": kind, "allowed": ", ".join(allowed)},
            )
        return value

    return BeforeValidator(check)


@cache
def _time_zones() -> frozenset[str]:
    return frozenset(available_timezones())


def _anchor(value: str) -> str:
    if ANCHOR_LOCAL.fullmatch(value) is None:
        raise PydanticCustomError("invalid", "must be HH:MM, from 00:00 to 23:59")
    return value


def _time_zone(value: str) -> str:
    if value not in _time_zones():
        raise PydanticCustomError("invalid", "not an IANA time zone")
    return value


def _paid_max(value: object) -> object:
    if value == "auto":
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise PydanticCustomError("invalid", 'must be "auto" or an integer')
    if not 1 <= value <= 100:
        raise PydanticCustomError("out_of_range", 'must be "auto" or an integer from 1 to 100')
    return value


SupportedModel = Annotated[Model, _supported("model", MODELS)]
SupportedEffort = Annotated[Effort, _supported("effort", _EFFORTS)]
Industry = Annotated[str, AfterValidator(_industry)]  # stored case-folded
Keyword = Annotated[str, Field(min_length=2, max_length=60)]
CompanyKey = Annotated[str, Field(min_length=1), AfterValidator(_company)]  # stored as the company key


def _all[T](values: tuple[T, ...]) -> Callable[[], list[T]]:
    return lambda: list(values)


# --- Schedule and Display ---


class Schedule(_Group):
    enabled: bool = True
    interval_hours: int = Field(6, ge=1, le=168)
    # converted once at save into `anchor_utc`, on the save date in `display.timezone`
    # (`seed.local_anchor_to_utc`)
    anchor_local: Annotated[str, AfterValidator(_anchor)] = "06:00"
    include_paid: bool = True  # scheduled runs; "Run now" has its own checkbox


class Display(_Group):
    timezone: Annotated[str, AfterValidator(_time_zone)] = "Europe/Berlin"  # IANA zone, display only
    ui_language: Literal["en", "de"] = "en"


# --- Scope ---


class ScopeLocation(_Group):
    """A gazetteer place (§8.12), chosen through the gazetteer search."""

    label: str = Field(min_length=1)
    gazetteer_id: str = Field(min_length=1)
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    radius_km: int = Field(ge=1, le=200)  # 0 is refused: distance bands divide by it


class SeniorityRange(_Group):
    mode: Literal["profile_relative", "explicit"] = "profile_relative"
    below: int = Field(1, ge=0, le=3)
    above: int = Field(1, ge=0, le=3)
    min: SeniorityLevel | None = None  # an open bound when null
    max: SeniorityLevel | None = None


class KnockoutSeverity(_Group):
    """The severity per §11.4 knockout code; `mandatory_language` follows `language_policy` (§13.6)."""

    degree: Severity = "soft"
    certification: Severity = "soft"
    security_clearance: Severity = "hard"
    work_permit: Severity = "hard"
    onsite_required: Severity = "hard"
    contract_type: Severity = "hard"


class ScopeVersionPayload(_Group):
    """The §13.1 group Scope without `max_ad_age_days`: a `scope_versions.payload`."""

    locations: list[ScopeLocation] = Field(default_factory=list, max_length=10)
    remote_within_germany: bool = True
    work_modes: Annotated[list[WorkMode], _set_of(get_args(WorkMode))] = Field(
        default_factory=_all(get_args(WorkMode)), min_length=1
    )
    employment_types: Annotated[list[EmploymentType], _set_of(get_args(EmploymentType))] = Field(
        default_factory=_all(get_args(EmploymentType)), min_length=1
    )
    seniority_range: SeniorityRange = Field(default_factory=SeniorityRange)
    industries_include: list[Industry] = Field(default_factory=list, max_length=30)  # compared case-folded
    industries_exclude: list[Industry] = Field(default_factory=list, max_length=30)
    accepted_ad_languages: Annotated[list[AdLanguage], _set_of(get_args(AdLanguage))] = Field(
        default_factory=_all(_DEFAULT_AD_LANGUAGES), min_length=1
    )
    language_policy_default: Severity = "hard"
    language_policy: dict[LanguageCode, Severity] = Field(default_factory=dict)
    excluded_companies: list[CompanyKey] = Field(default_factory=list, max_length=200)  # company keys
    excluded_keywords: list[Keyword] = Field(default_factory=list, max_length=100)
    exclude_keywords_in_description: bool = False
    min_salary_eur_year: Annotated[int, Field(ge=0, le=500_000)] | None = None
    knockout_severity: KnockoutSeverity = Field(default_factory=KnockoutSeverity)


class Scope(ScopeVersionPayload):
    max_ad_age_days: int = Field(30, ge=1, le=90)  # an admission field, not stored with the scope version


# --- Critic routing (the evaluation policy, §11.7.4) ---


class Evaluation(_Group):
    relevance_threshold: int = Field(60, ge=0, le=100)
    t2_band: int = Field(5, ge=0, le=20)
    # a rate: at most 2 decimal places, no trailing zero
    t2_straddle_max_coverage: Annotated[Decimal, _DECIMAL, _rate("0", "1"), _AS_STRING] = Decimal("0.7")
    t3_min_mid: int = Field(80, ge=50, le=100)
    t8_sample_pct: int = Field(5, ge=0, le=100)
    critic_light_triggers: Annotated[list[LightTrigger], _set_of(get_args(LightTrigger))] = Field(
        default_factory=_all(_DEFAULT_LIGHT_TRIGGERS)
    )


# --- Collection ---


class SourceSwitches(_Group):
    jsearch: bool = True
    serpapi: bool = True
    adzuna: bool = True
    arbeitnow: bool = True
    ats: bool = True


class Sources(_Group):
    enabled: SourceSwitches = Field(default_factory=SourceSwitches)


class QueryEntry(_Group):
    query_id: int = Field(ge=1, le=_BIGINT_MAX)  # the texts live in `queries`
    enabled: bool


class WatchlistEntry(_Group):
    watchlist_company_id: int = Field(ge=1, le=_BIGINT_MAX)
    enabled: bool


class Collection(_Group):
    stale_ttl_days: Annotated[int, Field(ge=7, le=90)] | None = None  # null: `scope.max_ad_age_days`


class Quota(_Group):
    """The quota limits (§6); the upper bounds fit the pacing columns' `numeric(10,4)` (M1 Carry-forward
    9)."""

    jsearch_monthly_limit: int = Field(200, ge=1, le=100_000)
    serpapi_monthly_limit: int = Field(250, ge=1, le=100_000)
    reserve_pct: int = Field(10, ge=0, le=50)
    serpapi_hourly_cap: int = Field(45, ge=1, le=49)
    adzuna_cap_minute: int = Field(20, ge=1, le=25)
    adzuna_cap_day: int = Field(225, ge=1, le=250)
    adzuna_cap_week: int = Field(900, ge=1, le=1_000)
    adzuna_cap_month: int = Field(2_250, ge=1, le=2_500)
    # `auto` = max(10, ceil(2 x fair_share)) (§6.4)
    paid_max_requests_per_run: Annotated[int | Literal["auto"], BeforeValidator(_paid_max)] = "auto"


# --- Scoring & models ---


class NodeChoice(_Group):
    model: SupportedModel
    effort: SupportedEffort


def _choice(model: Model, effort: Effort = "medium") -> Callable[[], NodeChoice]:
    return lambda: NodeChoice(model=model, effort=effort)


class ModelPolicyNodes(_Group):
    """Model policy B, the default (§10.3)."""

    generate: NodeChoice = Field(default_factory=_choice("claude-sonnet-5-5"))
    revise: NodeChoice = Field(default_factory=_choice("claude-sonnet-5-5"))
    critic: NodeChoice = Field(default_factory=_choice("claude-opus-5-5"))
    extract_profile: NodeChoice = Field(default_factory=_choice("claude-opus-5-5"))
    propose_queries: NodeChoice = Field(default_factory=_choice("claude-opus-5-5"))

    def by_node(self) -> dict[Node, NodeChoice]:
        return {node: cast(NodeChoice, getattr(self, node)) for node in NODES}


class ModelPolicy(_Group):
    preset: Literal["A", "B", "C", "custom"] = "B"
    nodes: ModelPolicyNodes = Field(default_factory=ModelPolicyNodes)
    critic_light_effort: SupportedEffort = "low"  # the critic's effort for a light round (§11.7.4)


class ExecutionNode(_Group):
    max_tokens: int = Field(ge=1_024, le=32_000)
    max_tokens_retry: int = Field(ge=1_024, le=64_000)  # and ≥ max_tokens (validate)
    deadline_s: int = Field(ge=120, le=1_800)


def _stage(first: int, enlarged: int, deadline_s: int) -> Callable[[], ExecutionNode]:
    return lambda: ExecutionNode(max_tokens=first, max_tokens_retry=enlarged, deadline_s=deadline_s)


class ExecutionConfig(_Group):
    """The §10.3 table."""

    generate: ExecutionNode = Field(default_factory=_stage(6_000, 12_000, 600))
    revise: ExecutionNode = Field(default_factory=_stage(6_000, 12_000, 600))
    critic: ExecutionNode = Field(default_factory=_stage(6_000, 12_000, 600))
    extract_profile: ExecutionNode = Field(default_factory=_stage(16_000, 32_000, 900))
    propose_queries: ExecutionNode = Field(default_factory=_stage(8_000, 16_000, 300))

    def by_node(self) -> dict[Node, ExecutionNode]:
        return {node: cast(ExecutionNode, getattr(self, node)) for node in NODES}


class Scoring(_Group):
    model_policy: ModelPolicy = Field(default_factory=ModelPolicy)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    max_targets_per_cycle: int = Field(60, ge=1, le=500)
    min_cycle_targets: int = Field(10, ge=1, le=100)
    batch_bulk_rescore: bool = True
    batch_min_targets: int = Field(200, ge=200, le=100_000)
    batch_max_items: int = Field(500, ge=200, le=10_000)  # 500 since spec revision 3.4 (Deviation 34)
    # a rate: at most 2 decimal places, no trailing zero
    bulk_rescore_max_budget_share: Annotated[Decimal, _DECIMAL, _rate("0.1", "1.0"), _AS_STRING] = Decimal(
        "0.4"
    )


# --- Budget, Retention, Notifications ---


class Llm(_Group):
    # The USD amounts have a fixed scale of 2 decimal places.
    cap_usd: Annotated[Decimal, _DECIMAL, _usd("1.00", "10000.00"), _AS_STRING] = Decimal("100.00")
    nonscoring_reserve_pct: int = Field(5, ge=0, le=20)
    # and ≥ the §6.9.1 bound of the configured concurrency and `batch_max_items` (validate)
    overrun_buffer_usd: Annotated[Decimal, _DECIMAL, _usd("0.50", "50.00"), _AS_STRING] = Decimal("2.00")
    min_bucket_capacity_usd: Annotated[Decimal, _DECIMAL, _usd("0.10", "100.00"), _AS_STRING] = Decimal(
        "1.00"
    )


class Retention(_Group):
    score_history_months: int = Field(12, ge=1, le=36)


class Notifications(_Group):
    push_min_severity: Literal["warn", "critical"] = "warn"


class Settings(_Group):
    """The settings document, `settings_versions.payload` (§13.1)."""

    schedule: Schedule = Field(default_factory=Schedule)
    display: Display = Field(default_factory=Display)
    scope: Scope = Field(default_factory=Scope)
    evaluation: Evaluation = Field(default_factory=Evaluation)
    sources: Sources = Field(default_factory=Sources)
    queries: list[QueryEntry] = Field(default_factory=list, max_length=40)
    watchlist: list[WatchlistEntry] = Field(default_factory=list, max_length=100)
    collection: Collection = Field(default_factory=Collection)
    quota: Quota = Field(default_factory=Quota)
    scoring: Scoring = Field(default_factory=Scoring)
    llm: Llm = Field(default_factory=Llm)
    retention: Retention = Field(default_factory=Retention)
    notifications: Notifications = Field(default_factory=Notifications)

    def to_payload(self) -> dict[str, JSONValue]:
        """The complete document as JSON values: decimals as strings, every key present."""
        return cast(dict[str, JSONValue], self.model_dump(mode="json"))
