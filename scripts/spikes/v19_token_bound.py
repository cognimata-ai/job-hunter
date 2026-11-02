"""Spike V19 (spec §10.7, §6.9.1, §22 V19, chair decision C1; plan Task 7): the text input bound and the
per-request overhead, measured with the free `messages.count_tokens`, plus one paid native-PDF extraction.

    uv run python scripts/spikes/v19_token_bound.py --dry-run | --count | --analyse | --live

- `--dry-run` sends nothing: it builds the synthetic corpus and prints its shape.
- `--count` (free): for every request of the corpus, `count_tokens` of the whole request (the gateway's
  `count_body`, the exact send payload) and of each component on its own (a request whose one user message is
  that component's text: each system block, each message, the `output_config` schema as canonical JSON). It
  also counts the native-PDF request and builds the recorder's request for it offline, which must be the
  counted body. Counts are kept in `.fixtures-raw/v19/` and never sent twice.
- `record_anthropic.py --spike v19 --dry-run|--live` sends the one native-PDF extraction (this module's
  `calls()`): the synthetic one-page PDF of `minimal_pdf`, budgeted at `pricing.worst_case_usd(model,
  input_tokens_bound=native_pdf_bound(model, count), max_tokens=1024, extra_usd=native_pdf_extra_usd(0.50))`.
- `--constants` (offline) writes the overhead constants alone, so that the paid extraction is reserved with
  them. `--analyse` (offline) applies the pure analysis below to the counts and to every recorded usage of
  Tasks 4 and 7 (`.fixtures-raw/anthropic-budget.json` and the raw request captures) and writes the V19 block
  of `bounds.py`: the constants and the native-PDF verdict. It writes nothing on a component with more tokens
  than bytes (the §10.7 factor rule is then applied by hand, and S3 restated) or on an uncovered request. It
  ends BLOCKED when the §6.9.1 overrun-buffer bound of the seeded settings exceeds their
  `overrun_buffer_usd`: validation, the default and `batch_max_items` are never changed to pass.
- `--live` runs `--count`, `--constants`, the recorder's dry run and live run of the native-PDF call, then
  `--analyse`.

**The corpus**: at least `MIN_REQUESTS_PER_MODEL` (30) synthetic requests per model of `pricing.MODELS`, in
German and English with umlauts, `ß`, typographic quotes, en dashes and ellipses: the profile-extraction
and query-proposal requests as `prompts.render` renders them, and the generator, critic and reviser requests
as `render.score_prompt` renders them from prompt-schema version 2 (M3 Task 4, R12): the shared rubric block
of rubric v2, the cached `<candidate_profile>` block, the role template, then the `<job_ad>` block, the
critic's with its `<draft>`, `<required_facts>` and `<validator_issues>`, the reviser's with its
`<previous_draft>` and `<issues>`; the generator's and the reviser's schema is candidate C and the critic's
the §11.7.3 one (`render.score_schema`). They carry no person's name and no contact data.

**The analysis** (pure; `tests/unit/test_v19_analysis.py`):
- every component must have tokens <= UTF-8 bytes (§1.5 S3). Otherwise the §10.7 factor is
  `byte_factor(violations)`, the smallest factor that restores the inequality with a 2x margin.
- A request's framing overhead is its whole count minus the sum of its components' counts.
- `PROMPT_OVERHEAD_TOKENS[model] = max(PROMPT_OVERHEAD_FLOOR, 2 x the model's largest framing overhead)`.
- The native-PDF path is enabled iff the extraction's usage (input plus both cache counts) is at most 1.25 x
  its count.
- `overrun_buffer_bound` is §6.9.1's `max_concurrent_llm_calls x PROMPT_OVERHEAD_TOKENS x the highest input
  price`: 3 score and 2 auxiliary online slots at the policy's costliest model, and `batch_max_items` batch
  slots of the generator model at batch prices, rounded up to the cent. It is the settings validation's
  formula (`settings.validate.overrun_buffer_bound`, plan Task 15) with the overhead constants under analysis.

Every printed line names counts, sizes, models and codes only, never a prompt or an output, and never the
key (read from `JOBHUNTER_ANTHROPIC_API_KEY` by `record_anthropic.counting_gateway`).
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import anthropic
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import bounds, fake
from jobhunter.llm.bounds import PROMPT_OVERHEAD_FLOOR
from jobhunter.llm.gateway import LLMGateway, NodeConfig, RenderedPrompt, WireSchema
from jobhunter.llm.pricing import MODELS, Model
from jobhunter.llm.prompts import render
from jobhunter.llm.schemas import SCHEMAS_V1
from jobhunter.llm.schemas.generator import GeneratorDraft
from jobhunter.llm.schemas.profile import ConfirmedProfile, ProfileOut
from jobhunter.scoring.facts import derive_fact_ids
from jobhunter.scoring.rubric import RUBRIC_V2, load_rubric
from jobhunter.settings import validate
from jobhunter.settings.model import Settings
from jobhunter.types import JSONValue, get_object

HERE: Final = Path(__file__).resolve().parent
REPO: Final = HERE.parents[1]
RAW: Final = REPO / ".fixtures-raw/v19"
BOUNDS_PY: Final = REPO / "backend/src/jobhunter/llm/bounds.py"
KEY_VAR: Final = "JOBHUNTER_ANTHROPIC_API_KEY"

MIN_REQUESTS_PER_MODEL: Final = 30
BLOCK_START: Final = (
    "# --- spike V19: scripts/spikes/v19_token_bound.py writes this block (docs/spikes/V19.md) ---"
)
BLOCK_END: Final = "# --- end of the spike V19 block ---"

# The paid call: a native-PDF profile extraction on `extract_profile`'s model, small and cheap.
NATIVE_CASE: Final = "native_pdf"
NATIVE_PURPOSE: Final = "v19_native_pdf"  # its own fixture directory: Task 13 owns `extract_profile`'s
NATIVE_MODEL: Final[Model] = "claude-opus-5-5"
NATIVE_EFFORT: Final = "low"
NATIVE_MAX_TOKENS: Final = 1024
NATIVE_BUFFER_USD: Final = Decimal("0.50")  # the §13.1 minimum overrun buffer
NATIVE_COUNT_FILE: Final = RAW / "native-pdf.count.json"
COUNTS_FILE: Final = RAW / "counts.json"
ANALYSIS_FILE: Final = RAW / "analysis.json"


# ---- the pure analysis ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Component:
    """One rendered component: its name (`system[0]`, `messages[0]`, `schema`), UTF-8 bytes and tokens."""

    name: str
    bytes: int
    tokens: int


@dataclass(frozen=True)
class Measured:
    """One counted request: the whole request's tokens and each component's."""

    model: str
    kind: str
    lang: str
    whole: int
    components: tuple[Component, ...]

    @property
    def framing(self) -> int:
        """The framing overhead: the whole request's tokens minus the sum of its components' tokens."""
        return self.whole - sum(c.tokens for c in self.components)

    @property
    def bytes(self) -> int:
        return sum(c.bytes for c in self.components)


@dataclass(frozen=True)
class Violation:
    """A component with more tokens than UTF-8 bytes."""

    model: str
    kind: str
    component: str
    tokens: int
    bytes: int


@dataclass(frozen=True)
class Analysis:
    requests: Mapping[str, int]
    max_framing: Mapping[str, int]
    overhead: Mapping[str, int]
    violations: tuple[Violation, ...]
    max_tokens_per_byte: Fraction
    byte_factor: Fraction | None = field(default=None)

    @property
    def tokens_within_bytes(self) -> bool:
        return not self.violations


def overhead_tokens(max_framing: int, *, floor: int = PROMPT_OVERHEAD_FLOOR) -> int:
    """`max(floor, 2 x max_framing)`: twice the largest framing overhead, never below the floor (§10.7)."""
    return max(floor, 2 * max_framing)


def byte_factor(violations: Iterable[Violation]) -> Fraction:
    """The §10.7 factor rule: the smallest factor `f` with `tokens ≤ f x bytes / 2` for every violating
    component, i.e. twice the largest tokens-per-byte ratio."""
    return 2 * max(Fraction(v.tokens, v.bytes) for v in violations)


def analyse(measured: Sequence[Measured]) -> Analysis:
    requests: dict[str, int] = {}
    max_framing: dict[str, int] = {}
    violations: list[Violation] = []
    ratio = Fraction(0)
    for m in measured:
        requests[m.model] = requests.get(m.model, 0) + 1
        max_framing[m.model] = max(max_framing.get(m.model, m.framing), m.framing)
        for c in m.components:
            if c.bytes:
                ratio = max(ratio, Fraction(c.tokens, c.bytes))
            if c.tokens > c.bytes:
                violations.append(Violation(m.model, m.kind, c.name, c.tokens, c.bytes))
    return Analysis(
        requests=requests,
        max_framing=max_framing,
        overhead={model: overhead_tokens(framing) for model, framing in max_framing.items()},
        violations=tuple(violations),
        max_tokens_per_byte=ratio,
        byte_factor=byte_factor(violations) if violations else None,
    )


def overhead_constants(analysis: Analysis) -> dict[str, int]:
    """The `PROMPT_OVERHEAD_TOKENS` of every model; refused while a model has fewer than 30 requests, or while
    a component has more tokens than bytes (the §10.7 factor rule then comes first)."""
    if analysis.violations:
        raise ValueError("a component has tokens > bytes: apply the §10.7 factor rule and restate S3 first")
    short = [m for m in MODELS if analysis.requests.get(m, 0) < MIN_REQUESTS_PER_MODEL]
    if short:
        raise ValueError(f"fewer than {MIN_REQUESTS_PER_MODEL} counted requests for {', '.join(short)}")
    return {model: analysis.overhead[model] for model in MODELS}


def native_pdf_within(*, used: int, counted: int) -> bool:
    """`used ≤ 1.25 x counted`, exactly (§10.7)."""
    return 4 * used <= 5 * counted


@dataclass(frozen=True)
class RecordedUsage:
    """A recorded response's usage (input plus both cache counts) and the bound its request has now."""

    spike: str
    case: str
    model: str
    used: int
    bound: int
    native_pdf: bool


def usage_breaches(recorded: Iterable[RecordedUsage]) -> list[RecordedUsage]:
    return [r for r in recorded if r.used > r.bound]


def overrun_buffer_bound(settings: Mapping[str, Any], overhead: Mapping[Model, int]) -> Decimal:
    """§6.9.1's lower bound on `overrun_buffer_usd` for the settings document `settings`, with the overhead
    constants `overhead`: the settings validation's own formula (`settings.validate.overrun_buffer_bound`)."""
    return validate.overrun_buffer_bound(Settings.model_validate(dict(settings)), overhead=overhead)


def rewrite_bounds(source: str, constants: Mapping[str, int], *, native_pdf_enabled: bool) -> str:
    """`bounds.py` with its V19 block holding `constants` and `native_pdf_enabled`; nothing else changes."""
    if set(constants) != set(MODELS):
        raise ValueError("the constants must name every model")
    if low := [m for m in MODELS if constants[m] < PROMPT_OVERHEAD_FLOOR]:
        raise ValueError(f"an overhead below the floor of {PROMPT_OVERHEAD_FLOOR}: {', '.join(low)}")
    head, start, rest = source.partition(BLOCK_START)
    _, end, tail = rest.partition(BLOCK_END)
    if not start or not end:
        raise ValueError("bounds.py has no V19 block")
    lines = [
        BLOCK_START,
        "PROMPT_OVERHEAD_TOKENS: Final[Mapping[Model, int]] = MappingProxyType(",
        "    {",
        *(f'        "{model}": {constants[model]:_},' for model in MODELS),
        "    }",
        ")",
        f"NATIVE_PDF_ENABLED: Final[bool] = {native_pdf_enabled}",
        BLOCK_END,
    ]
    return head + "\n".join(lines) + tail


# ---- the synthetic corpus ---------------------------------------------------------------------------------

_DE: Final = (
    "Wir suchen eine:n erfahrene:n Backend-Entwickler:in für unser Plattform-Team in München \u2013 remote "
    "möglich.",
    "Du entwickelst skalierbare Services in Python und PostgreSQL; \u201eQualität\u201c ist für dich "
    "selbstverständlich\u2026",
    "Sehr gute Deutsch- und gute Englischkenntnisse (mindestens C1) sowie Erfahrung mit Kubernetes sind von "
    "Vorteil.",
    "Unser Büro liegt zentral an der Hauptstraße; flexible Arbeitszeiten, 30 Tage Urlaub und ein Zuschuss "
    "zum Deutschlandticket.",
    "Größere Datenmengen verarbeitest du zuverlässig, und Störungen im Betrieb löst du gemeinsam mit dem "
    "Team \u2013 schnell, ruhig und gründlich.",
    "Wir übernehmen Verantwortung für \u201eunsere\u201c Systeme: Code-Reviews, Tests und Monitoring gehören "
    "dazu\u2026",
)
_EN: Final = (
    "We are looking for an experienced backend engineer to join our platform team in Berlin \u2013 hybrid, "
    "two days on site.",
    "You will design \u201creliable\u201d services in Python and PostgreSQL\u2026 and you care about clear, "
    "tested code.",
    "Fluent German (C1) and English are required; experience with Kubernetes and event-driven systems is a "
    "plus.",
    "Our office is close to the main station; we offer flexible hours, 30 days of leave and a training "
    "budget.",
    "You handle large volumes of data reliably and solve incidents together with the team \u2013 calmly and "
    "thoroughly.",
    "We own \u201cour\u201d systems end to end: reviews, tests and monitoring are part of the job\u2026",
)
_SIZES: Final = (300, 1_500, 6_000, 15_000)  # characters of the varying text
_LOCATIONS: Final = ("München", "Köln", "Düsseldorf")


def _text(lang: str, size: int, offset: int = 0) -> str:
    """Synthetic text of about `size` characters, from the sentences of `lang`, in numbered paragraphs."""
    sentences = _DE if lang == "de" else _EN
    parts: list[str] = []
    i = offset
    while sum(len(p) + 1 for p in parts) < size:
        parts.append(f"{i // len(sentences) + 1}. {sentences[i % len(sentences)]}")
        i += 1
    return "\n".join(parts)


def _profile(lang: str) -> dict[str, Any]:
    de = lang == "de"
    return {
        "summary": (
            "Backend-Entwickler mit sechs Jahren Erfahrung in Python und PostgreSQL; zuletzt "
            "Sendungsverfolgung für Logistik \u2013 \u201everlässlich\u201c und gründlich\u2026"
            if de
            else "Backend engineer with six years of Python and PostgreSQL, most recently shipment tracking "
            "for logistics \u2013 \u201creliable\u201d and thorough\u2026"
        ),
        "target_titles": [{"title": "Backend-Entwickler" if de else "Backend Engineer", "lang": lang}],
        "seniority": "senior",
        "total_years_experience": 6,
        "roles": [
            {
                "title": "Backend Engineer",
                "employer": "Beispiel Logistik GmbH",
                "start": "2019-03-01",
                "end": "present",
                "seniority": "senior",
                "domain": "Logistik" if de else "logistics",
                "highlights": [
                    "Sendungsverfolgung in Python und PostgreSQL entwickelt"
                    if de
                    else "Built shipment tracking in Python and PostgreSQL"
                ],
            },
            {
                "title": "Softwareentwickler" if de else "Software developer",
                "employer": "Musterdaten AG",
                "start": "2016-09-01",
                "end": "2019-02-01",
                "seniority": "junior",
                "domain": None,
                "highlights": ["Datenimporte in Python" if de else "Data imports in Python"],
            },
        ],
        "skills": [
            {
                "name": "Python",
                "category": "programming_language",
                "level": "expert",
                "years": 6,
                "evidence": "Python",
            },
            {
                "name": "PostgreSQL",
                "category": "tool",
                "level": "advanced",
                "years": 5,
                "evidence": "PostgreSQL",
            },
            {
                "name": "Kubernetes",
                "category": "platform",
                "level": "basic",
                "years": None,
                "evidence": "Kubernetes",
            },
        ],
        "languages": [
            {"code": "de", "code_other": None, "cefr": "native", "evidence": "Deutsch (Muttersprache)"},
            {"code": "en", "code_other": None, "cefr": "C1", "evidence": "Englisch (C1)"},
        ],
        "education": [{"level": "bachelor", "field": "Informatik", "institution": None, "year": 2016}],
        "certifications": [],
        "domains": ["Logistik" if de else "logistics"],
    }


def v2_version() -> render.PromptVersion:
    """Prompt-schema version 2 as the seed stores it (built offline; the id is never sent)."""
    templates, schemas = render.prompt_schema_v2()
    return render.PromptVersion(
        id=2,
        prompt_schema_hash=render.prompt_schema_hash(templates, schemas),
        templates={name: str(text) for name, text in templates.items()},
        schemas=schemas,
        parser_family="generator_v2_items",
    )


V2: Final = v2_version()


def _draft(lang: str) -> GeneratorDraft:
    """A synthetic internal draft for the critic's `<draft>`, with a mandatory language that binds."""
    de = lang == "de"
    quote = "sehr gute Deutschkenntnisse" if de else "Fluent German (C1)"
    quoted: dict[str, JSONValue] = {"evidence": [quote]}
    criterion: dict[str, JSONValue] = {
        "level": 3,
        "basis": "observed",
        "ad_evidence": [quote],
        "profile_evidence": [{"section": "languages", "quote": "Deutsch (Muttersprache)"}],
        "rationale": "Deutsch \u2013 Muttersprache; Englisch C1."
        if de
        else "German is native \u2013 as asked.",
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
                    {"language": "de", "language_other": None, "level": "B2", "mandatory": True, **quoted}
                ],
                "must_haves": [{"text": "Python", **quoted}],
                "nice_to_haves": [{"text": "Kubernetes", **quoted}],
                "seniority": {"value": "senior", **quoted},
                "min_years_experience": {"value": None, "evidence": []},
                "industry": {"value": None, "evidence": []},
                "employment_types": {"values": ["full_time"], **quoted},
                "degree": {
                    "level": "unspecified",
                    "mandatory": False,
                    "or_equivalent": False,
                    "field": None,
                    "evidence": [],
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
            "summary_en": "A good fit for the backend role\u2026",
            "summary_de": "Gute Passung für die Backend-Rolle\u2026",
        }
    )


@dataclass(frozen=True)
class Request:
    """One corpus request: its node, its prompt and what it is."""

    kind: str
    lang: str
    size: int
    node: NodeConfig
    prompt: RenderedPrompt

    def components(self) -> list[tuple[str, str]]:
        """`(name, text)` of every byte-bounded component (`RenderedPrompt.as_request`)."""
        rendered = self.prompt.as_request(self.node.schema)
        named = [(f"system[{i}]", text) for i, text in enumerate(rendered.system)]
        named += [(f"messages[{i}]", text) for i, text in enumerate(rendered.messages)]
        return [*named, ("schema", rendered.schema_json)]


# The corpus's kinds, in their order: the two single calls, then the three score roles.
KINDS: Final = ("profile", "queries", "generator", "critic", "revise")
_KIND_PURPOSE: Final = {
    "profile": "extract_profile",
    "queries": "propose_queries",
    "generator": "generate",
    "critic": "critic",
    "revise": "revise",
}
_KIND_SCHEMA: Final = {"profile": "profile", "queries": "queries"}
_SCORE_ROLE: Final[Mapping[str, render.ScoreRole]] = {
    "generator": "generate",
    "critic": "critic",
    "revise": "revise",
}


def _schema(kind: str) -> WireSchema:
    if kind in _SCORE_ROLE:
        return render.score_schema(_SCORE_ROLE[kind], V2)
    return WireSchema.from_model(SCHEMAS_V1[_KIND_SCHEMA[kind]])


def _node(kind: str, model: Model) -> NodeConfig:
    return NodeConfig(
        purpose=_KIND_PURPOSE[kind],  # type: ignore[arg-type]
        model=model,
        effort="medium",
        max_tokens=4000,
        max_tokens_retry=8000,
        deadline_s=600,
        schema=_schema(kind),
    )


def _prompt(kind: str, lang: str, size: int) -> RenderedPrompt:
    if kind == "profile":
        return render.extract_profile_prompt(_text(lang, size, offset=1))
    if kind == "queries":
        profile = ConfirmedProfile.model_validate({**_profile(lang), "summary": _text(lang, size // 4 + 100)})
        return render.propose_queries_prompt(profile, _LOCATIONS[: 1 + _SIZES.index(size) % 3])
    ad = render.job_ad_block(
        title="Backend Engineer (Python)",
        company="Beispiel Logistik GmbH",
        locations=list(_LOCATIONS[: 1 + _SIZES.index(size) % 3]),
        work_modes=[("hybrid", "structured"), ("remote", "text")],
        employment_types=[("full_time", "structured")],
        completeness="full",
        description_text=_text(lang, size, offset=2),
    )
    common: dict[str, Any] = {
        "version": V2,
        "rubric": load_rubric(RUBRIC_V2),
        "profile": ConfirmedProfile.model_validate(_profile(lang)),
        "ad": ad,
    }
    if kind == "generator":
        return render.score_prompt("generate", **common)
    draft = _draft(lang)
    facts = derive_fact_ids(draft.job_facts)
    issues = [render.Issue("v1_limit", "skills"), render.Issue("ungrounded_quote", "languages", ("C1",))]
    if kind == "critic":
        return render.score_prompt(
            "critic",
            **common,
            draft=render.draft_block(draft, facts),
            required_facts=render.required_facts_block(list(facts.values())),
            validator_issues=render.issues_block(issues),
        )
    # the reviser's issues: the validator's, a grounded critic dispute and an unconfirmed required fact
    issues += [
        render.Issue("level_too_high", "languages", ("C1",), suggested_level=2),
        render.Issue("fact_uncertain", "mandatory_language:de"),
    ]
    return render.score_prompt(
        "revise",
        **common,
        previous_draft=render.draft_block(draft, facts),
        issues=render.issues_block(issues),
    )


def corpus(model: Model) -> list[Request]:
    """The model's synthetic requests: every kind in German and English at every size (5 x 2 x 4 = 40)."""
    return [
        Request(kind, lang, size, _node(kind, model), _prompt(kind, lang, size))
        for kind in KINDS
        for lang in ("de", "en")
        for size in _SIZES
    ]


# ---- the native-PDF request -------------------------------------------------------------------------------

_PDF_LINES: Final = (
    "Lebenslauf (synthetisch)",
    "Berufserfahrung",
    "03/2019 - heute: Backend Engineer, Beispiel Logistik GmbH, Hamburg",
    "Sendungsverfolgung mit Python und PostgreSQL",
    "09/2016 - 02/2019: Softwareentwickler, Musterdaten AG, Bremen",
    "Datenimporte in Python",
    "Ausbildung: 2016 B.Sc. Informatik",
    "Sprachen: Deutsch (Muttersprache), Englisch (C1)",
    "Kenntnisse: Python, PostgreSQL, Docker",
)


def _pdf_string(text: str) -> str:
    return "(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def minimal_pdf(lines: Sequence[str] = _PDF_LINES) -> bytes:
    """A one-page PDF 1.4 with the ASCII `lines` in Helvetica, no name and no contact data."""
    stream = (
        "BT /F1 11 Tf 14 TL 72 770 Td " + " ".join(f"{_pdf_string(line)} Tj T*" for line in lines) + " ET"
    )
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1")
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode("latin-1")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode(
        "latin-1"
    )
    return bytes(out)


def native_node() -> NodeConfig:
    return NodeConfig(
        purpose="extract_profile",
        model=NATIVE_MODEL,
        effort=NATIVE_EFFORT,
        max_tokens=NATIVE_MAX_TOKENS,
        max_tokens_retry=NATIVE_MAX_TOKENS,
        deadline_s=900,
        schema=WireSchema.from_model(ProfileOut),
    )


def native_prompt() -> RenderedPrompt:
    return render.extract_profile_native_pdf_prompt(minimal_pdf())


def _native_count() -> dict[str, Any]:
    if not NATIVE_COUNT_FILE.is_file():
        raise FileNotFoundError("count the native-PDF request first: v19_token_bound.py --count")
    doc = json.loads(NATIVE_COUNT_FILE.read_text("utf-8"))
    if not isinstance(doc, dict):
        raise TypeError("the native-PDF count file holds no object")
    return doc


# ---- the recorder's spike definition ----------------------------------------------------------------------


def calls() -> list[dict[str, Any]]:
    """The one paid call: the native-PDF extraction, bounded by its count (`--count` keeps it)."""
    prompt = native_prompt()
    (message,) = prompt.messages
    return [
        {
            "case": NATIVE_CASE,
            "purpose": NATIVE_PURPOSE,
            "model": NATIVE_MODEL,
            "effort": NATIVE_EFFORT,
            "max_tokens": NATIVE_MAX_TOKENS,
            "system": [block.text for block in prompt.system],
            "user": list(message.content),
            "schema": ProfileOut,
            "native_pdf_count_tokens": int(_native_count()["input_tokens"]),
            "extra_usd": str(bounds.native_pdf_extra_usd(NATIVE_BUFFER_USD)),
        }
    ]


def check(observation: dict[str, Any]) -> dict[str, Any]:
    """The extraction's verdict: its usage against 1.25 x its count (counts and flags only)."""
    if "events" not in observation:
        return {
            "missing": ["http_200"],
            "http_status": observation["status"],
            "error_type": observation["error_type"],
        }
    usage = observation["usage_metadata"]
    meta = observation["response_metadata"]
    used = int(usage.get("input_tokens") or 0)  # langchain's input_tokens includes both cache counts (V13)
    counted = int(_native_count()["input_tokens"])
    checks = {
        "end_turn": meta.get("stop_reason") == "end_turn",
        "model_served": meta.get("model_name") == observation["requested_model"],
        "usage_within_1_25_count": native_pdf_within(used=used, counted=counted),
    }
    return {
        "missing": [name for name, ok in checks.items() if not ok],
        "checks": checks,
        "stop_reason": meta.get("stop_reason"),
        "usage_input_with_cache": used,
        "count_tokens": counted,
        "output_tokens": usage.get("output_tokens"),
    }


# ---- counting (free) --------------------------------------------------------------------------------------


def _recorder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("jobhunter_spike_recorder", HERE / "record_anthropic.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("the recorder is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


class Counter:
    """`count_tokens` with a file cache (a request is never counted twice) and a patient retry of 429s."""

    def __init__(self, gateway: LLMGateway, cache: dict[str, Any]) -> None:
        self.gateway = gateway
        self.cache = cache
        self.sent = 0

    async def count(self, model: Model, body: Mapping[str, Any]) -> int:
        key = _sha(model, json.dumps(body, sort_keys=True, ensure_ascii=False))
        if key in self.cache:
            return int(self.cache[key])
        for attempt in range(8):
            try:
                self.sent += 1
                counted = await self.gateway.count_tokens(model, body)
                break
            except anthropic.RateLimitError as exc:
                wait = float(exc.response.headers.get("retry-after") or 5 * (attempt + 1))
                await asyncio.sleep(min(wait, 60))
        else:
            raise RuntimeError("count_tokens stayed rate limited")
        self.cache[key] = counted
        _write_json(COUNTS_FILE, self.cache)
        return counted

    async def component(self, model: Model, text: str) -> int:
        return await self.count(model, {"messages": [{"role": "user", "content": text}]})


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n", "utf-8")
    tmp.replace(path)


async def count_all(key: str, out: Any) -> None:
    recorder = _recorder()
    cache: dict[str, Any] = json.loads(COUNTS_FILE.read_text("utf-8")) if COUNTS_FILE.is_file() else {}
    measured: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    async with recorder.counting_gateway(key) as gateway:
        counter = Counter(gateway, cache)
        for model in MODELS:
            try:
                for request in corpus(model):
                    whole = await counter.count(model, gateway.count_body(request.node, request.prompt))
                    components = [
                        {
                            "name": name,
                            "bytes": len(text.encode("utf-8")),
                            "tokens": await counter.component(model, text),
                        }
                        for name, text in request.components()
                    ]
                    measured.append(
                        {
                            "model": model,
                            "kind": request.kind,
                            "lang": request.lang,
                            "size": request.size,
                            "whole": whole,
                            "components": components,
                        }
                    )
            except anthropic.APIStatusError as exc:
                failures[model] = f"http_{exc.status_code}"
                print(f"v19 {model}: count_tokens failed with {failures[model]}", file=out, flush=True)
                continue
            print(
                f"v19 {model}: {sum(1 for m in measured if m['model'] == model)} requests counted", file=out
            )
        node, prompt = native_node(), native_prompt()
        body = gateway.count_body(node, prompt)
        native = await counter.count(NATIVE_MODEL, body)
        _write_json(
            NATIVE_COUNT_FILE,
            {
                "model": NATIVE_MODEL,
                "input_tokens": native,
                "body_sha256": _sha(json.dumps(body, sort_keys=True)),
            },
        )
        print(f"v19 native PDF ({len(minimal_pdf())} bytes): count_tokens {native}", file=out)
        # The recorder's request for it, built offline, must be the counted body (plus max_tokens, stream).
        planned = recorder.plan("v19", sys.modules[__name__])
        cfg = recorder._worker_config(key)
        outcome = await recorder._send(planned[0], cfg, recorder._AnswersLocally())
        sent = dict(outcome.capture.request["body"])
        same = {k: v for k, v in sent.items() if k not in ("max_tokens", "stream")} == json.loads(
            json.dumps({**body, "model": NATIVE_MODEL})
        )
        print(f"v19 native PDF: the recorder sends the counted body: {same}", file=out)
        if not same:
            raise RuntimeError("the recorder's native-PDF request is not the counted one")
    _write_json(RAW / "measured.json", {"measured": measured, "failures": failures})
    print(f"v19: {counter.sent} count_tokens requests sent (free), {len(cache)} counts kept", file=out)


# ---- the analysis of the counts and the recordings --------------------------------------------------------


def _loaded_measured() -> list[Measured]:
    doc = json.loads((RAW / "measured.json").read_text("utf-8"))
    return [
        Measured(
            m["model"],
            m["kind"],
            m["lang"],
            int(m["whole"]),
            tuple(Component(c["name"], int(c["bytes"]), int(c["tokens"])) for c in m["components"]),
        )
        for m in doc["measured"]
    ]


def _raw_components(body: Mapping[str, Any]) -> tuple[list[str], list[str], str]:
    system = body.get("system") or []
    systems = [system] if isinstance(system, str) else [str(b.get("text", "")) for b in system]
    messages = []
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            messages.append(content)
        else:
            messages.append("".join(str(b.get("text", "")) for b in content if b.get("type") == "text"))
    schema = fake.canonical_schema_json(body["output_config"]["format"]["schema"])
    return systems, messages, schema


def recorded_usages(overhead: Mapping[str, int]) -> list[RecordedUsage]:
    """Every recorded response of Tasks 4 and 7 with a usage, bounded with `overhead`."""
    budget = json.loads((REPO / ".fixtures-raw/anthropic-budget.json").read_text("utf-8"))
    out: list[RecordedUsage] = []
    for entry in budget["entries"]:
        usage = entry.get("usage")
        if not usage:
            continue
        used = (
            int(usage["input_tokens"])
            + int(usage["cache_creation_input_tokens"])
            + int(usage["cache_read_input_tokens"])
        )
        model = entry["model"]
        if entry["spike"] == "v19":
            counted = int(_native_count()["input_tokens"])
            bound = math.ceil(Fraction(5, 4) * counted) + overhead[model]
            out.append(RecordedUsage(entry["spike"], entry["case"], model, used, bound, True))
            continue
        raw = json.loads(
            (REPO / ".fixtures-raw/anthropic" / entry["spike"] / f"{entry['case']}.request.json").read_text(
                "utf-8"
            )
        )
        systems, messages, schema = _raw_components(raw["body"])
        size = sum(len(t.encode("utf-8")) for t in [*systems, *messages, schema])
        out.append(RecordedUsage(entry["spike"], entry["case"], model, used, size + overhead[model], False))
    return out


def analyse_all(out: Any, *, constants_only: bool = False) -> int:
    """The analysis of the counts and the recordings (module docstring), in the brief's order: the overhead
    constants into `bounds.py`, the native-PDF verdict (once the extraction is recorded; `constants_only`
    keeps the current one, for the extraction is reserved with the new constants), then the §6.9.1 stop rule.
    A violated inequality or an uncovered request writes nothing."""
    measured = _loaded_measured()
    analysis = analyse(measured)
    summary: dict[str, Any] = {
        "requests": dict(analysis.requests),
        "max_framing": dict(analysis.max_framing),
        "overhead": dict(analysis.overhead),
        "max_tokens_per_byte": str(analysis.max_tokens_per_byte),
        "violations": [v.__dict__ for v in analysis.violations],
    }
    for model in analysis.requests:
        mine = [m for m in measured if m.model == model]
        summary.setdefault("min_framing", {})[model] = min(m.framing for m in mine)
        summary.setdefault("max_whole_over_bytes", {})[model] = str(
            max(Fraction(m.whole, m.bytes) for m in mine)
        )
        by_kind = {}
        for kind in KINDS:
            framings = [m.framing for m in mine if m.kind == kind]
            by_kind[kind] = [min(framings), max(framings)]
        summary.setdefault("framing_by_kind", {})[model] = by_kind
    print(json.dumps(summary, indent=1, ensure_ascii=False), file=out)
    constants = overhead_constants(analysis)  # refuses on a violation or a short model
    # Every corpus request is covered by its new bound, and so is every recorded usage.
    uncovered = [m for m in measured if m.bytes + constants[m.model] < m.whole]
    margin = min(m.bytes + constants[m.model] - m.whole for m in measured)
    recorded = recorded_usages(constants)
    breaches = usage_breaches(recorded)
    native = [r for r in recorded if r.native_pdf]
    counted = int(_native_count()["input_tokens"])
    native_ok = bool(native) and all(native_pdf_within(used=r.used, counted=counted) for r in native)
    if constants_only or not native:
        native_ok = bounds.NATIVE_PDF_ENABLED  # no verdict yet: the flag stays as it is
    settings = load_settings_v1()
    buffer_bound = overrun_buffer_bound(settings, constants)
    seeded_buffer = Decimal(str(get_object(settings, "llm")["overrun_buffer_usd"]))
    result = {
        **summary,
        "constants": constants,
        "uncovered_requests": len(uncovered),
        "smallest_bound_margin": margin,
        "recorded": [r.__dict__ for r in recorded],
        "breaches": [r.__dict__ for r in breaches],
        "native_pdf_count_tokens": counted,
        "native_pdf_verdict": None if not native or constants_only else native_ok,
        "overrun_buffer_bound_usd": str(buffer_bound),
        "seeded_overrun_buffer_usd": str(seeded_buffer),
    }
    _write_json(ANALYSIS_FILE, result)
    print(
        f"v19 constants {constants}; uncovered requests {len(uncovered)} (smallest margin {margin}); "
        f"recorded usages {len(recorded)}, above their bound {len(breaches)}; native PDF enabled "
        f"{native_ok}; overrun-buffer bound ${buffer_bound} against the seeded ${seeded_buffer}",
        file=out,
    )
    if uncovered or breaches:
        print("v19 STOP: a bound does not cover its request; bounds.py is not written", file=out)
        return 5
    BOUNDS_PY.write_text(
        rewrite_bounds(BOUNDS_PY.read_text("utf-8"), constants, native_pdf_enabled=native_ok), "utf-8"
    )
    print(f"v19: wrote {BOUNDS_PY.relative_to(REPO)}", file=out)
    if constants_only:
        return 0
    if buffer_bound > seeded_buffer:
        print(
            f"v19 BLOCKED: the §6.9.1 overrun-buffer bound ${buffer_bound} exceeds the seeded "
            f"overrun_buffer_usd ${seeded_buffer}; validation, the default and batch_max_items are unchanged",
            file=out,
        )
        return 6
    return 0


# ---- the command ------------------------------------------------------------------------------------------


def _summary(out: Any) -> None:
    for model in MODELS:
        requests = corpus(model)
        sizes = [sum(len(t.encode("utf-8")) for _, t in r.components()) for r in requests]
        print(f"v19 {model}: {len(requests)} requests, {min(sizes)}..{max(sizes)} bytes", file=out)
    print(f"v19 native PDF: {len(minimal_pdf())} bytes, {NATIVE_MODEL} effort {NATIVE_EFFORT}", file=out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="v19_token_bound.py", description=(__doc__ or "").split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("--dry-run", "--count", "--constants", "--analyse", "--live"):
        mode.add_argument(flag, action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    out = sys.stdout
    if args.dry_run:
        _summary(out)
        return 0
    if args.analyse:
        return analyse_all(out)
    if args.constants:
        return analyse_all(out, constants_only=True)
    key = os.environ.get(KEY_VAR, "").strip()
    if not key:
        print(f"v19: REFUSED, {KEY_VAR} is not set; nothing sent", file=out)
        return 4
    try:
        asyncio.run(count_all(key, out))
    except Exception as exc:  # noqa: BLE001 - the class only: an SDK error holds its request
        print(f"v19: counting failed ({type(exc).__name__})", file=out)
        return 1
    if args.count:
        return 0
    if (code := analyse_all(out, constants_only=True)) != 0:
        return code
    recorder = _recorder()
    for step in ("--dry-run", "--live"):
        if (code := recorder.main(["--spike", "v19", step], environ=os.environ)) != 0:
            print(f"v19: the recorder's {step} ended {code}; nothing more", file=out)
            return code
    return analyse_all(out)


if __name__ == "__main__":
    # As the recorder: no ANTHROPIC_* variable may steer a request (G1).
    for name in [name for name in os.environ if name.startswith("ANTHROPIC_")]:
        del os.environ[name]
    raise SystemExit(main())
