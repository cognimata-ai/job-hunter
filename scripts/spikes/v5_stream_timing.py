"""Spike V5's paid part, with V14 folded in (spec §22 L6657, L6666; M2 Deviation 9; M3 Task 26, Codex r1 #40,
#41): 20 streamed calls through the real gateway, measuring their timing and their cache behaviour.

    uv run python scripts/spikes/record_anthropic.py --spike v5 --dry-run|--live [--through-gateway]
    uv run python scripts/spikes/record_anthropic.py --spike v14 --dry-run|--live [--through-gateway]

Both are gateway spikes of the budgeted recorder (`record_anthropic.GATEWAY_SPIKES`): each call is
`LLMGateway.call()` on the recorder's throwaway database, sent through `BudgetedTransport(TimingTransport(
network))`, so every HTTP attempt is authorised and reserved before it leaves (`SPIKE_CALLS`: v5 30, its 20
calls and up to 10 enlarged `max_tokens` stages, Ruling T26-c; v14 2) and every SSE event carries its arrival
time. The calls run one at a time, as an eval run's non-scoring calls (an `eval_runs` row of kind `canary`
owns them), at the seeded policy's models, efforts, `max_tokens` stages and deadlines.

**The calls** (`gateway_calls`), every one rendered by `render.score_prompt` from prompt-schema version 2 in
the real three-block layout: `system[0]` the shared rubric block of rubric v2, `system[1]` the synthetic
`<candidate_profile>` block with the one cache marker, `system[2]` the role template; one user turn with the
`<job_ad>` block (and, for the critic, a synthetic `<draft>`, `<required_facts>` and `<validator_issues>`).
- `v5`: 5 pairs `k`, each `g<k>a`, `g<k>b` (the generator, `claude-sonnet-5-5`, schema C) then `c<k>a`,
  `c<k>b` (the critic, `claude-opus-5-5`, the §11.7.3 schema): the two calls of a pair are the same request,
  the second sent at once after the first (well within the 5-minute TTL), and the generator and the critic of
  pair `k` share its prefix. Each pair has its own synthetic profile (so its first calls start cold) and its
  own synthetic ad. The prefix (rubric and profile) is at least `PREFIX_MIN_TOKENS` at chars/4.
- `v14`: one pair on a sixth prefix, `g6` (the generator on Sonnet/`medium`) then `x6`, a call with the critic
  prompt and schema on Sonnet/`medium` (A3 G6: whether two schemas on one (model, effort) share an entry).

**What is measured** (`run_gateway`): per call its duration from the send to the settled result (`call_s`)
and per HTTP attempt its stream duration, its time to the first event, the largest gap between two SSE
events, its usage, its stop reason, its `max_tokens` stage and its thinking tokens (`AttemptTiming`).
`analyse` holds the acceptance: the p99 (the largest of the 20) call under 300 s, every gap under 30 s, and
the second call of each pair reading at least 4,000 cached tokens (V14), with every call measured once (a
retried call reduces the sample: it is reported). It also reports each model's attempts and how many were
cut at `max_tokens`, the first generator call on its own (it may include the grammar compile, A3 G28),
whether a critic call on Opus read the Sonnet generator's entry (V14: it should not, another model), and the
cache-creation counts by role (G7: the generator and the critic of a pair cache the same prefix bytes under
different schemas, so equal counts put the schema's framing outside the cached part). For v5, free
`count_tokens` requests then split the first generator and critic requests into their components (the V19
method: the whole request, and each component as the one user message), for the token figures Ruling T25-a
asks for. The spike stops at the first refusal of an attempt and reports what it measured.

**What is committed**: every completed stream as `<purpose>/<model>/<schema12>.<case>.sse` with its sidecars,
and for v5 the second calls of the first pair (`g1b`, `c1b`) also where the dev stack's fake serves the
`generate` and `critic` calls (`<schema12>.sse`, Carry T8-c), and `g1b` where it serves `revise` (the reviser
has the generator's model and schema; V5 sends no revise call, and the sidecar names both purposes). Every
value here is synthetic: invented candidates and employers, no names, no contact data.
"""

import dataclasses
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import anthropic
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import fake
from jobhunter.llm.errors import InvalidOutput, LLMError, LLMRequestError, LLMTransient
from jobhunter.llm.gateway import CallContext, NodeConfig, RenderedPrompt, node_config
from jobhunter.llm.prompts import render
from jobhunter.llm.prompts.render import JobAdBlock, estimated_tokens
from jobhunter.llm.schemas.generator import GeneratorDraft
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.llm.spend_ledger import EvalRunOwner
from jobhunter.scoring.facts import derive_fact_ids
from jobhunter.scoring.rubric import RUBRIC_V2, load_rubric
from jobhunter.types import JSONValue, get_object

SPIKES: Final = ("v5", "v14")
PAIRS: Final = 5
SONNET: Final = "claude-sonnet-5-5"
PREFIX_MIN_TOKENS: Final = 4_000  # the prefix up to the breakpoint, at chars/4 (L3880)
ACCEPT_MAX_CALL_S: Final = 300.0
ACCEPT_MAX_GAP_S: Final = 30.0
ACCEPT_MIN_CACHE_READ: Final = 4_000
CRITERIA: Final = ("p99_under_300_s", "gaps_under_30_s", "second_of_pair_reads_cache", "all_measured")
PRIMARY: Final = ("g1b", "c1b")  # committed where the fake serves `generate` and `critic` too
# The reviser shares the generator's model and schema (§10.3), and V5 sends no revise call: the dev stack's
# fake answers `revise` with the generator's recorded stream (Carry T8-c).
REVISE_FROM: Final = "g1b"
_SETTINGS: Final = load_settings_v1()
POLICY: Final[Mapping[str, JSONValue]] = get_object(_SETTINGS, "scoring.model_policy")
EXECUTION: Final[Mapping[str, JSONValue]] = get_object(_SETTINGS, "scoring.execution")


def _v2() -> render.PromptVersion:
    """Prompt-schema version 2 as the seed stores it (built offline; the id is never sent)."""
    templates, schemas = render.prompt_schema_v2()
    return render.PromptVersion(
        id=2,
        prompt_schema_hash=render.prompt_schema_hash(templates, schemas),
        templates={name: str(text) for name, text in templates.items()},
        schemas=schemas,
        parser_family="generator_v2_items",
    )


V2: Final = _v2()

# ---- the synthetic inputs ----------------------------------------------------------------------------------

# One invented field per prefix: the German and English domain, the product, and an invented employer.
_FIELDS: Final = (
    ("Logistik", "logistics", "Sendungsverfolgung", "shipment tracking", "Beispiel Logistik GmbH"),
    ("Energie", "energy", "Netzleitstelle", "grid operations", "Muster Energie AG"),
    ("Versicherung", "insurance", "Schadenbearbeitung", "claims handling", "Exempla Versicherung AG"),
    ("Nahverkehr", "public transport", "Fahrgastinformation", "passenger information", "Probe Verkehr GmbH"),
    ("Lebensmittelhandel", "food retail", "Warenwirtschaft", "stock management", "Demo Frischmarkt GmbH"),
    ("Medizintechnik", "medical devices", "Gerätedaten", "device data", "Fiktiv Medizintechnik GmbH"),
)
_SKILLS: Final = (
    ("Python", "programming_language", "expert", 7),
    ("PostgreSQL", "tool", "advanced", 6),
    ("FastAPI", "framework", "advanced", 4),
    ("SQLAlchemy", "framework", "advanced", 5),
    ("Kafka", "tool", "intermediate", 3),
    ("Kubernetes", "platform", "intermediate", 3),
    ("Docker", "tool", "advanced", 6),
    ("Terraform", "tool", "intermediate", 2),
    ("Go", "programming_language", "intermediate", 2),
    ("TypeScript", "programming_language", "intermediate", 3),
    ("Redis", "tool", "advanced", 4),
    ("gRPC", "framework", "intermediate", 2),
    ("Domain-Driven Design", "method", "advanced", 4),
    ("Code-Reviews", "method", "expert", 7),
    ("Testautomatisierung", "method", "advanced", 6),
    ("Observability", "domain_knowledge", "advanced", 4),
    ("Mentoring", "soft_skill", "advanced", 3),
    ("Moderation von Workshops", "soft_skill", "intermediate", 2),
)


def profile(k: int) -> ConfirmedProfile:
    """Pair `k`'s synthetic confirmed profile: an invented backend engineer in field `k`, with no name and no
    contact data, long enough that the prefix passes `PREFIX_MIN_TOKENS`."""
    de, en, product_de, product_en, employer = _FIELDS[k - 1]
    roles: list[dict[str, Any]] = []
    for i, (title, start, end, seniority) in enumerate(
        (
            ("Senior Backend Engineer", "2021-04-01", "present", "senior"),
            ("Backend Engineer", "2018-09-01", "2021-03-01", "mid"),
            ("Softwareentwickler", "2016-02-01", "2018-08-01", "junior"),
            ("Werkstudent Softwareentwicklung", "2014-10-01", "2016-01-01", "working_student"),
        )
    ):
        roles.append(
            {
                "title": title,
                "employer": employer if i == 0 else f"Muster{'daten' if i % 2 else 'werk'} {i} GmbH",
                "start": start,
                "end": end,
                "seniority": seniority,
                "domain": de,
                "highlights": [
                    f"{product_de}: Dienste in Python und PostgreSQL entworfen und betrieben ({i + 1}).",
                    f"Built {product_en} services in Python with an event pipeline on Kafka ({i + 1}).",
                    f"Lasttests und Monitoring für die {product_de} eingeführt; Ausfälle deutlich seltener.",
                    f"Introduced code reviews, test automation and on-call runbooks for the {en} platform.",
                    f"Datenmodelle für {de} mit Fachbereich und Betrieb abgestimmt und migriert.",
                ],
            }
        )
    return ConfirmedProfile.model_validate(
        {
            "summary": (
                f"Backend-Entwicklerin mit zehn Jahren Erfahrung, zuletzt in der {product_de} ({de}); "
                f"backend engineer for {en}: Python, PostgreSQL, event-driven systems (synthetic, {k})."
            ),
            "target_titles": [
                {"title": "Senior Backend Engineer", "lang": "en"},
                {"title": "Backend-Entwicklerin", "lang": "de"},
                {"title": "Platform Engineer", "lang": "en"},
            ],
            "seniority": "senior",
            "total_years_experience": 10,
            "roles": roles,
            "skills": [
                {
                    "name": name,
                    "category": category,
                    "level": level,
                    "years": years,
                    "evidence": f"{name} ({years} Jahre, {de})",
                }
                for name, category, level, years in _SKILLS
            ],
            "languages": [
                {"code": "de", "code_other": None, "cefr": "native", "evidence": "Deutsch (Muttersprache)"},
                {"code": "en", "code_other": None, "cefr": "C1", "evidence": "Englisch (C1)"},
                {"code": "fr", "code_other": None, "cefr": "B1", "evidence": "Französisch (B1)"},
            ],
            "education": [
                {"level": "master", "field": "Informatik", "institution": None, "year": 2016},
                {"level": "bachelor", "field": "Wirtschaftsinformatik", "institution": None, "year": 2014},
            ],
            "certifications": [
                {"name": "Certified Kubernetes Application Developer", "issuer": None, "year": 2022},
                {"name": "Professional Scrum Master I", "issuer": None, "year": 2020},
            ],
            "domains": [de, "Plattformbetrieb", "Datenintegration"],
        }
    )


_AD_SENTENCES: Final = (
    "Wir suchen eine erfahrene Person für die Backend-Entwicklung unserer {product}.",
    "Du entwickelst und betreibst Dienste in Python und PostgreSQL, bis in die Produktion.",
    "Unsere Plattform verarbeitet täglich viele Ereignisse aus dem Bereich {field}; Zuverlässigkeit zählt.",
    "Mindestens fünf Jahre Erfahrung in der Backend-Entwicklung mit Python setzen wir voraus.",
    "Sehr gute Deutschkenntnisse und gute Englischkenntnisse (mindestens C1) sind Voraussetzung.",
    "Erfahrung mit Kafka, Kubernetes und Terraform ist von Vorteil, aber kein Muss.",
    "Du arbeitest eng mit Fachbereich, Betrieb und Produktmanagement zusammen und moderierst Workshops.",
    "Code-Reviews, Testautomatisierung und Monitoring sind für dich selbstverständlich.",
    "Ein abgeschlossenes Studium der Informatik oder eine vergleichbare Qualifikation wird erwartet.",
    "Wir bieten hybrides Arbeiten mit zwei Bürotagen pro Woche, 30 Tage Urlaub und ein Weiterbildungsbudget.",
    "We are an international team; English is our working language in architecture discussions.",
    "You will shape the {product_en} roadmap together with two other senior engineers and a product owner.",
)


def ad(k: int) -> JobAdBlock:
    """Pair `k`'s synthetic ad (an invented employer and role, no contact data), about 3,000 characters."""
    de, _, product_de, product_en, _ = _FIELDS[k - 1]
    company = f"Synthetisch {de} Dienste GmbH"
    paragraphs = []
    for round_no in range(2):
        for i, sentence in enumerate(_AD_SENTENCES):
            text = sentence.format(product=product_de, field=de, product_en=product_en)
            paragraphs.append(f"{round_no * len(_AD_SENTENCES) + i + 1}. {text}")
    return render.job_ad_block(
        title=f"Senior Backend Engineer (Python) {product_de}",
        company=company,
        locations=["Hamburg", "Remote (Deutschland)"],
        work_modes=[("hybrid", "structured")],
        employment_types=[("full_time", "structured")],
        completeness="full",
        description_text="\n".join(paragraphs),
    )


def _draft(k: int) -> GeneratorDraft:
    """Pair `k`'s synthetic internal draft for the critic's `<draft>`, quoting its ad."""
    quoted: dict[str, JSONValue] = {"evidence": ["Python und PostgreSQL"]}
    language: dict[str, JSONValue] = {"evidence": ["Sehr gute Deutschkenntnisse"]}
    criterion: dict[str, JSONValue] = {
        "level": 3,
        "basis": "observed",
        "ad_evidence": ["Dienste in Python und PostgreSQL"],
        "profile_evidence": [{"section": "skills", "quote": "Python"}],
        "rationale": f"Die Kernkenntnisse sind belegt (synthetischer Entwurf {k}).",
    }
    return GeneratorDraft.model_validate(
        {
            "not_a_job_ad": False,
            "injection_observed": False,
            "job_facts": {
                "work_mode": {"value": "hybrid", **quoted},
                "onsite_only": {"asserted": False, "evidence": []},
                "remote_region": {"value": "unspecified", "countries": [], "evidence": []},
                "required_languages": [
                    {"language": "de", "language_other": None, "level": "C1", "mandatory": True, **language},
                    {"language": "en", "language_other": None, "level": "C1", "mandatory": True, **language},
                ],
                "must_haves": [{"text": "Python", **quoted}],
                "nice_to_haves": [{"text": "Kafka", "evidence": ["Erfahrung mit Kafka"]}],
                "seniority": {"value": "senior", **quoted},
                "min_years_experience": {"value": 5, "evidence": ["Mindestens fünf Jahre Erfahrung"]},
                "industry": {"value": _FIELDS[k - 1][0], "evidence": []},
                "employment_types": {"values": ["full_time"], **quoted},
                "degree": {
                    "level": "bachelor",
                    "mandatory": False,
                    "or_equivalent": True,
                    "field": "Informatik",
                    "evidence": ["Studium der Informatik oder eine vergleichbare Qualifikation"],
                },
                "certifications": [],
                "security_clearance": {"required": False, "level": None, "evidence": []},
                "work_permit": {"required": False, "evidence": []},
            },
            "criteria": {
                key: criterion
                for key in (
                    "role_fit",
                    "skills",
                    "experience_seniority",
                    "domain",
                    "languages",
                    "education_certifications",
                )
            },
            "summary_en": f"A good fit for the synthetic backend role {k}.",
            "summary_de": f"Gute Passung für die synthetische Backend-Rolle {k}.",
        }
    )


def _common(k: int) -> dict[str, Any]:
    return {"version": V2, "rubric": load_rubric(RUBRIC_V2), "profile": profile(k), "ad": ad(k)}


def generator_prompt(k: int) -> RenderedPrompt:
    return render.score_prompt("generate", **_common(k))


def critic_prompt(k: int) -> RenderedPrompt:
    draft = _draft(k)
    facts = derive_fact_ids(draft.job_facts)
    issues = [render.Issue("v1_limit", "skills"), render.Issue("ungrounded_quote", "languages", ("C1",))]
    return render.score_prompt(
        "critic",
        **_common(k),
        draft=render.draft_block(draft, facts),
        required_facts=render.required_facts_block(list(facts.values())),
        validator_issues=render.issues_block(issues),
    )


def _node(role: render.ScoreRole) -> NodeConfig:
    return node_config(role, model_policy=POLICY, execution=EXECUTION, schema=render.score_schema(role, V2))


def prefix_tokens(prompt: RenderedPrompt) -> int:
    """The prefix up to the cache breakpoint, at chars/4."""
    marked = [i for i, block in enumerate(prompt.system) if block.cache]
    return estimated_tokens("".join(block.text for block in prompt.system[: marked[-1] + 1]))


def gateway_calls(spike: str) -> list[dict[str, Any]]:
    """The spike's calls in their order (module docstring): `case`, `node`, `prompt`, `role`, `pair` and
    `position`."""
    calls: list[dict[str, Any]] = []
    if spike == "v5":
        generator, critic = _node("generate"), _node("critic")
        for k in range(1, PAIRS + 1):
            for role, node, prompt in (
                ("g", generator, generator_prompt(k)),
                ("c", critic, critic_prompt(k)),
            ):
                for position in "ab":
                    calls.append(
                        {
                            "case": f"{role}{k}{position}",
                            "node": node,
                            "prompt": prompt,
                            "role": node.purpose,
                            "pair": k,
                            "position": position,
                        }
                    )
    elif spike == "v14":
        k = PAIRS + 1
        sonnet_critic = dataclasses.replace(_node("critic"), model=SONNET)
        calls = [
            {"case": f"g{k}", "node": _node("generate"), "prompt": generator_prompt(k), "role": "generate"},
            {"case": f"x{k}", "node": sonnet_critic, "prompt": critic_prompt(k), "role": "critic"},
        ]
        for call, position in zip(calls, "ab", strict=True):
            call |= {"pair": k, "position": position}
    else:
        raise ValueError(f"no spike {spike!r} here")
    for call in calls:
        if prefix_tokens(call["prompt"]) < PREFIX_MIN_TOKENS:
            raise ValueError(
                f"{spike} {call['case']}: the prefix is under {PREFIX_MIN_TOKENS} tokens at chars/4"
            )
    return calls


# ---- what is measured --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptTiming:
    """One HTTP attempt: its case (`<call>#<n>`), status, stream duration, time to its first SSE event, the
    largest gap between two events, its event count, its usage and its stop reason (all seconds)."""

    case: str
    status: int | None
    duration_s: float | None
    ttfe_s: float | None
    max_gap_s: float | None
    events: int
    usage: dict[str, int] | None
    stop_reason: str | None
    thinking_tokens: int | None = None
    max_tokens: int | None = None  # the stage the attempt was sent at (6,000 first, 12,000 enlarged)


@dataclass(frozen=True)
class CallTiming:
    """One logical call: its case, role, pair and position, model and effort, outcome (`ok` or the gateway's
    error code), the time from its send to its settled result, and its attempts in order."""

    case: str
    role: str
    pair: int
    position: str
    model: str
    effort: str
    outcome: str
    call_s: float
    attempts: tuple[AttemptTiming, ...]

    @property
    def final(self) -> AttemptTiming | None:
        return self.attempts[-1] if self.attempts else None

    def count(self, name: str) -> int | None:
        final = self.final
        return None if final is None or final.usage is None else int(final.usage.get(name) or 0)


def _usage(events: Sequence[fake.SSEEvent]) -> dict[str, int] | None:
    """The response's usage: `message_delta`'s cumulative counts, `message_start`'s for what it lacks."""
    start = next((e.data["message"].get("usage", {}) for e in events if e.name == "message_start"), {})
    delta = next((e.data.get("usage", {}) for e in events if e.name == "message_delta"), None)
    if delta is None:
        return None
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    return {k: int(delta.get(k) if delta.get(k) is not None else start.get(k) or 0) for k in keys}


def _thinking_tokens(events: Sequence[fake.SSEEvent]) -> int | None:
    """The thinking tokens of the output (`usage.output_tokens_details.thinking_tokens`), final first."""
    for event in reversed(events):
        if event.name == "message_delta":
            details = (event.data.get("usage") or {}).get("output_tokens_details") or {}
            if details.get("thinking_tokens") is not None:
                return int(details["thinking_tokens"])
    return None


def attempt_timing(exchange: Any) -> AttemptTiming:
    """An attempt's figures from the recorder's `Exchange`."""
    timed = exchange.event_times()
    events = [event for _, event in timed]
    stop = next((e.data["delta"].get("stop_reason") for e in events if e.name == "message_delta"), None)
    started, ended = exchange.started, exchange.ended
    body = exchange.request_body() or {}
    return AttemptTiming(
        case=str(exchange.case),
        status=exchange.status,
        duration_s=None if ended is None else ended - started,
        ttfe_s=timed[0][0] - started if timed else None,
        max_gap_s=exchange.max_gap_s(),
        events=len(events),
        usage=_usage(events),
        stop_reason=stop,
        thinking_tokens=_thinking_tokens(events),
        max_tokens=None if body.get("max_tokens") is None else int(body["max_tokens"]),
    )


def analyse(calls: Sequence[CallTiming], *, expected: int = 2 * 2 * PAIRS) -> dict[str, Any]:
    """The acceptance and the cache findings of a run (module docstring); numbers, flags and cases only."""
    measured = [c for c in calls if c.outcome == "ok"]
    gaps = [a.max_gap_s for c in calls for a in c.attempts if a.max_gap_s is not None]
    second = {c.case: c.count("cache_read_input_tokens") for c in measured if c.position == "b"}
    firsts = [c for c in calls if c.position == "a"]
    p99 = max((c.call_s for c in measured), default=None)
    max_gap = max(gaps, default=None)
    checks = {
        "p99_under_300_s": p99 is not None and p99 < ACCEPT_MAX_CALL_S,
        "gaps_under_30_s": max_gap is not None and max_gap < ACCEPT_MAX_GAP_S,
        "second_of_pair_reads_cache": bool(second)
        and all((n or 0) >= ACCEPT_MIN_CACHE_READ for n in second.values()),
        "all_measured": len(measured) == expected,
    }
    first_generator = next((c for c in calls if c.role == "generate"), None)
    by_model: dict[str, dict[str, int]] = {}
    for c in calls:
        for a in c.attempts:
            if a.stop_reason is None:
                continue  # no answer that ended (an error response, a cut stream)
            counts = by_model.setdefault(c.model, {"attempts": 0, "max_tokens": 0})
            counts["attempts"] += 1
            counts["max_tokens"] += a.stop_reason == "max_tokens"
    return {
        "accepted": all(checks.values()),
        "failed": [name for name in CRITERIA if not checks[name]],
        "checks": checks,
        "measured": len(measured),
        "expected": expected,
        "attempts": sum(len(c.attempts) for c in calls),
        "retried": {c.case: len(c.attempts) for c in calls if len(c.attempts) > 1},
        "outcomes": {c.case: c.outcome for c in calls if c.outcome != "ok"},
        "p99_call_s": p99,
        "max_gap_s": max_gap,
        "first_generator_call_s": None if first_generator is None else first_generator.call_s,
        "first_generator_ttfe_s": None
        if first_generator is None or not first_generator.attempts
        else first_generator.attempts[0].ttfe_s,
        "max_tokens_by_model": by_model,
        "stop_reasons": {c.case: [a.stop_reason for a in c.attempts] for c in calls},
        "thinking_tokens": {c.case: [a.thinking_tokens for a in c.attempts] for c in calls},
        "call_s": {c.case: c.call_s for c in calls},
        "ttfe_s": {c.case: c.final.ttfe_s for c in calls if c.final is not None},
        "second_of_pair_cache_read": second,
        "critic_read_generator_entry": any(
            (c.count("cache_read_input_tokens") or 0) > 0 for c in firsts if c.role == "critic"
        ),
        "cache_creation_by_role": {
            role: [c.count("cache_creation_input_tokens") for c in firsts if c.role == role]
            for role in ("generate", "critic")
        },
        "usage": {c.case: None if c.final is None else c.final.usage for c in calls},
    }


# ---- the live run ------------------------------------------------------------------------------------------

_EVAL_RUN: Final = (
    "INSERT INTO eval_runs (kind, route, prompt_schema_version_id, model_policy_id, execution_config_id, "
    "status) VALUES ('canary', 'production', %(p)s, %(m)s, %(e)s, 'running') RETURNING id"
)
_VERSION_HASH: Final = "SELECT prompt_schema_hash FROM prompt_schema_versions WHERE id = %(id)s"
_PAYLOADS: Final = (
    "SELECT m.payload AS model_policy, e.payload AS execution FROM model_policies m, execution_configs e "
    "WHERE m.id = %(m)s AND e.id = %(e)s"
)


def _code(exc: LLMError) -> str:
    """The gateway outcome of a failed call, a code only."""
    if isinstance(exc, LLMRequestError):
        return f"http_{exc.status}"
    if isinstance(exc, LLMTransient):
        return f"transient_{exc.code}"
    if isinstance(exc, InvalidOutput):
        return f"invalid_output_{exc.code}"
    return type(exc).__name__


async def _seeded(session: Any) -> tuple[dict[str, int], str | None]:
    """The throwaway database's active ids, and why its configuration is not the planned one (or None)."""
    ids = await session.active_ids()
    async with session.engine.connect() as conn:
        digest = (await conn.exec_driver_sql(_VERSION_HASH, {"id": ids["prompt_schema_version_id"]})).scalar()
        payloads = (
            await conn.exec_driver_sql(
                _PAYLOADS, {"m": ids["model_policy_id"], "e": ids["execution_config_id"]}
            )
        ).one()
    if bytes(digest or b"") != V2.prompt_schema_hash:
        return ids, "the active prompt-schema version is not version 2"
    for role in ("generate", "critic"):
        if get_object(payloads.model_policy, f"nodes.{role}") != get_object(POLICY, f"nodes.{role}"):
            return ids, f"the active model policy's {role} node is not the seeded one"
        if get_object(payloads.execution, role) != get_object(EXECUTION, role):
            return ids, f"the active execution config's {role} stages are not the seeded ones"
    return ids, None


async def _split(session: Any, call: Any) -> dict[str, Any] | None:
    """The V19 split of one request on the free counting endpoint: the whole request, each component as the
    one user message, and the framing (whole minus the components); None when a count fails."""
    gateway, node = session.gateway, call.node
    rendered = call.prompt.as_request(node.schema)
    components = [(f"system[{i}]", text) for i, text in enumerate(rendered.system)]
    components += [(f"messages[{i}]", text) for i, text in enumerate(rendered.messages)]
    components.append(("schema", rendered.schema_json))
    try:
        whole = await gateway.count_tokens(node.model, gateway.count_body(node, call.prompt))
        counts = {
            name: await gateway.count_tokens(node.model, {"messages": [{"role": "user", "content": text}]})
            for name, text in components
        }
    except anthropic.APIError as exc:
        session.say(f"count_tokens failed ({type(exc).__name__}): no token split")
        return None
    return {
        "case": call.case,
        "whole": whole,
        "components": counts,
        "bytes": {name: len(text.encode("utf-8")) for name, text in components},
        "framing": whole - sum(counts.values()),
        "prefix": counts["system[0]"] + counts["system[1]"],
        "input_tokens_bound": call.input_tokens_bound(),
    }


async def run_gateway(spike: str, session: Any) -> dict[str, Any]:
    """The spike's calls one at a time on the session's gateway, then the analysis and the commits (module
    docstring)."""
    planned = {c["case"]: c for c in gateway_calls(spike)}
    ids, wrong = await _seeded(session)
    if wrong is not None:
        return {"ok": False, "lines": [f"STOP: {wrong}; nothing sent"]}
    async with session.engine.begin() as conn:
        params = {
            "p": ids["prompt_schema_version_id"],
            "m": ids["model_policy_id"],
            "e": ids["execution_config_id"],
        }
        eval_run_id = int((await conn.exec_driver_sql(_EVAL_RUN, params)).scalar_one())
    owner = EvalRunOwner(eval_run_id)
    measured: list[CallTiming] = []
    stop: str | None = None
    for call in session.calls:
        if session.counted(call.case):
            session.say(f"{call.case} was counted by an earlier run: not sent")
            continue
        node = call.node
        ctx = CallContext(
            owner=owner,
            purpose_class="nonscoring",
            execution_config_id=ids["execution_config_id"],
            deadline=session.gateway.monotonic() + node.deadline_s,
            eval_run_id=eval_run_id,
        )
        refusals = len(session.refusals)
        started = time.monotonic()
        with session.case(call.case):
            try:
                await session.gateway.call(node, call.prompt, ctx)
                outcome = "ok"
            except LLMError as exc:
                outcome = _code(exc)
        timing = CallTiming(
            case=call.case,
            role=str(planned[call.case]["role"]),
            pair=int(planned[call.case]["pair"]),
            position=str(planned[call.case]["position"]),
            model=node.model,
            effort=node.effort,
            outcome=outcome,
            call_s=time.monotonic() - started,
            attempts=tuple(attempt_timing(e) for e in session.exchanges(call.case)),
        )
        measured.append(timing)
        final = timing.final
        session.say(
            f"{call.case} {node.model} {outcome} call_s={timing.call_s:.1f} attempts={len(timing.attempts)} "
            f"stages={[a.max_tokens for a in timing.attempts]} "
            f"stops={[a.stop_reason for a in timing.attempts]} "
            f"thinking={[a.thinking_tokens for a in timing.attempts]} "
            f"ttfe_s={[a.ttfe_s for a in timing.attempts]} gaps_s={[a.max_gap_s for a in timing.attempts]} "
            f"max_gap_s={None if final is None else final.max_gap_s} "
            f"usage={None if final is None else final.usage}"
        )
        if len(session.refusals) > refusals or session.stopped is not None:
            stop = session.stopped or session.refusals[-1]
            break
    counts: dict[str, Any] = {}
    if spike == "v5" and stop is None:
        for role in ("generate", "critic"):
            first = next(c for c in session.calls if c.node.purpose == role)
            if (split := await _split(session, first)) is not None:
                counts[role] = split
    committed = []
    for timing in measured:
        exchanges = session.exchanges(timing.case)
        if not exchanges:
            continue
        targets: list[tuple[str | None, str | None]] = [(timing.case, None)]
        if spike == "v5" and timing.case in PRIMARY:
            targets.append((None, None))
        if spike == "v5" and timing.case == REVISE_FROM:
            targets.append((None, "revise"))
        for name, serves in targets:
            if (path := session.commit_stream(exchanges[-1], name=name, serves=serves)) is not None:
                committed.append(str(path.relative_to(session.recorder.fixtures_out)))
    analysis = analyse(measured, expected=len(planned))
    lines = [
        f"measured {analysis['measured']} of {analysis['expected']} calls in {analysis['attempts']} "
        "attempts; "
        f"accepted={analysis['accepted']} failed={analysis['failed']}",
        f"p99_call_s={analysis['p99_call_s']} max_gap_s={analysis['max_gap_s']} "
        f"first_generator_call_s={analysis['first_generator_call_s']} "
        f"first_generator_ttfe_s={analysis['first_generator_ttfe_s']}",
        f"max_tokens_by_model={analysis['max_tokens_by_model']}",
        f"second_of_pair_cache_read={analysis['second_of_pair_cache_read']}",
        f"cache_creation_by_role={analysis['cache_creation_by_role']} "
        f"critic_read_generator_entry={analysis['critic_read_generator_entry']}",
        *(f"token split {role}: {split}" for role, split in counts.items()),
        *(f"committed {path}" for path in committed),
    ]
    if stop is not None:
        lines.append(f"STOP at the first refusal: {stop}")
    return {
        "ok": stop is None and len(measured) == len(planned),
        "eval_run_id": eval_run_id,
        "calls": [dataclasses.asdict(t) for t in measured],
        "analysis": analysis,
        "counts": counts,
        "committed": committed,
        "stop": stop,
        "lines": lines,
    }
