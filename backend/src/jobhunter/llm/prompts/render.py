"""The prompt renderer (spec §10.6, §9.5, §5.5.1) and the prompt-schema versions (§7.3; M2 plan Deviation 17,
M3 plan Deviation 40).

**The single calls** (M2). Profile extraction and query proposal are one call each with no cache marker: one
static system block, the template as it is packaged, and one user turn that holds the input. Nothing dynamic
(a date, an id, the input) is ever in the system block.
- `extract_profile_prompt(reviewed_text)` puts the reviewed text inside `<cv_text>…</cv_text>` exactly, so
  the string inside the tags is the text whose sha256 the draft holds (`transmitted_text_sha256`, §9.5).
- `extract_profile_native_pdf_prompt(pdf)` sends the file as a native `document` block (§12.7), with the
  same template.
- `propose_queries_prompt(profile, locations)` sends the confirmed profile (`candidate_profile_block`) and
  the search locations as a JSON list.
Their `input_refs` are empty: the nodes add the ids they own. `template_version(name)` is `<name>@<the first
12 hex digits of the template's sha256>`, recorded on every call.

**The prompt-schema versions.** A version is its templates by name and its transformed schemas by name;
its `prompt_schema_hash` is `sha256(canonical_json(templates) ‖ canonical_json(schemas) ‖ RENDERER_VERSION)`,
where `RENDERER_VERSION` names the framing this module adds around the templates (the tags, the native-PDF
instruction, the `<job_ad>` layout and the rubric block's rendering), so a change to it is a new version.
- Version 1 (`prompt_schema_v1`) is frozen: the two templates of `TEMPLATE_NAMES_V1` and `schemas.SCHEMAS_V1`,
  and nothing else, so its hash is the one the M2 seed wrote.
- Version 2 (`prompt_schema_v2`) adds the four score templates (`TEMPLATE_NAMES_V2`) and has
  `schemas.SCHEMAS_V2`: the generator is candidate C (`GeneratorWire`); the two M2 templates and the critic,
  profile and queries schemas are version 1's, byte for byte (R12).
The packaged files are the source of the shipped rows only (`db.seed` writes them). After the seed the
database row is the authority: `load_prompt_version` reads a row into a `PromptVersion` (cached per process by
`(id, hash)`; a row is immutable), and every score render takes one, so the templates, the wire schema, the
mirror (`schemas.mirror_for` of the row's stored `parser_family`) and the recorded identity
(`template_version = f"{role}@{version.id}"`, `input_refs["prompt_schema_version_id"]`) all come from it. A
version of family `generator_v1` cannot score (`PromptVersionUnscorable`).

**The score prompts** (`score_prompt`, §10.6 L3860-3881):
- `system[0]` the shared rubric block (`rubric_block`: the version's `shared_rubric` template with the
  rubric's weights and anchors filled in), `system[1]` the `<candidate_profile>` block with the one cache
  breakpoint at its end, `system[2]` the role's template, after the breakpoint. Nothing before the breakpoint
  carries a date or an id: the same rubric and profile give the same prefix bytes.
- One user turn: the `<job_ad untrusted="true">` block (`job_ad_block`), then for the critic `<draft>`,
  `<required_facts>` and `<validator_issues>`, for the reviser `<previous_draft>` and `<issues>`. No
  assistant turn is ever sent.
- `score_schema(role, version)` is the node's `WireSchema`: the version's stored `generator` schema with its
  family's mirror for `generate` and `revise`, its `critic` schema with `CriticOut` for `critic`.
- `input_refs` holds `prompt_schema_version_id` and `truncated`; the caller adds `job_revision_id`,
  `profile_version_id` and `rubric_version_id`.

**Untrusted text never closes a block** (Controller Ruling T5-a). Every interpolated content (the `<job_ad>`
fields and description, the profile JSON, the critic's and the reviser's blocks, the search locations) has
each literal opening or closing tag of the prompt's own blocks (`BLOCK_NAMES`, any case, `</?name>` with
optional inner whitespace and, for an opening tag, attributes) neutralised by `neutralise_block_tags`: its
`<` and `>` become U+FF1C and U+FF1E, one code point for one, so every offset of the rendered text is the
source's and `fold` (NFKC) still reads the quote it was. The reviewed CV text inside `<cv_text>` is left
exactly as it is (its sha256 is the draft's `transmitted_text_sha256`, §9.5).

**The `<job_ad>` block** holds the structured fields (title, company, locations, the work modes and the
employment types each with its source, the completeness flag) and the description, cut to its first
`DESCRIPTION_HEAD` and last `DESCRIPTION_TAIL` characters with `TRUNCATION_MARKER` between them when it is
longer than both. Its `segments` are the origin map: every character of the text lies in exactly one segment,
in order; a `structured_field` segment is one field's line (its label, value and newline), a `marker` segment
is the block's own text (the tags, the description label and the truncation marker), and a description
segment's `source_start` is the offset of its first character in `job_revisions.description_text`, so a
quote found at `span_start` maps to `source_start + (span_start - block_start)` whatever the omitted middle's
length.
"""

import base64
import copy
import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from importlib import resources
from types import MappingProxyType
from typing import Any, Final, Literal, cast, get_args

from langchain_core.messages import HumanMessage
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.seed import canonical_json
from jobhunter.db.sql import q
from jobhunter.errors import DomainError
from jobhunter.llm.gateway import RenderedPrompt, SystemBlock, WireSchema
from jobhunter.llm.schemas import (
    PARSER_FAMILIES,
    SCHEMAS_V1,
    SCHEMAS_V2,
    ParserFamily,
    is_transform_fixed_point,
    mirror_for,
    transformed,
)
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.generator import GeneratorDraft
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.scoring.facts import FACT_ID_CODES, NOT_A_JOB_AD, FactRef, binding_facts, fact_id
from jobhunter.scoring.rubric import LLM_CRITERIA, Rubric
from jobhunter.types import JSONValue

RENDERER_VERSION: Final = "1"

TemplateName = Literal[
    "profile_extract", "propose_queries", "shared_rubric", "role_generate", "role_critic", "role_revise"
]
# Frozen: nothing adds to either (a release with other templates ships a new version).
TEMPLATE_NAMES_V1: Final[tuple[TemplateName, ...]] = ("profile_extract", "propose_queries")
TEMPLATE_NAMES_V2: Final[tuple[TemplateName, ...]] = (
    *TEMPLATE_NAMES_V1,
    "shared_rubric",
    "role_generate",
    "role_critic",
    "role_revise",
)

# The user turn's text after a native PDF document block (§12.7).
NATIVE_PDF_INSTRUCTION: Final = "The CV is the attached PDF document. Extract the candidate profile from it."


def template_text(name: TemplateName) -> str:
    """The packaged template `name` (`<name>.md`), exactly as stored."""
    if name not in get_args(TemplateName):
        raise ValueError(f"no prompt template {name!r}")
    return resources.files("jobhunter.llm.prompts").joinpath(f"{name}.md").read_text("utf-8")


def template_version(name: TemplateName) -> str:
    """`<name>@<sha256(template)[:12]>`: the `llm_calls.prompt_template_version` of a single call."""
    digest = hashlib.sha256(template_text(name).encode("utf-8")).hexdigest()
    return f"{name}@{digest[:12]}"


def _single_call(name: TemplateName, message: HumanMessage) -> RenderedPrompt:
    return RenderedPrompt(
        system=(SystemBlock(template_text(name)),),
        messages=(message,),
        template_version=template_version(name),
        input_refs={},
    )


def extract_profile_prompt(reviewed_text: str) -> RenderedPrompt:
    """The `extract_profile` request of a reviewed CV text: the text exactly inside `<cv_text>`."""
    return _single_call("profile_extract", HumanMessage(f"<cv_text>{reviewed_text}</cv_text>"))


def extract_profile_native_pdf_prompt(pdf: bytes) -> RenderedPrompt:
    """The `extract_profile` request of a native PDF (§12.7): a base64 `document` block, then a short text."""
    if not pdf:
        raise ValueError("an empty PDF")
    document = {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf).decode()},
    }
    message = HumanMessage([document, {"type": "text", "text": NATIVE_PDF_INSTRUCTION}])
    return _single_call("profile_extract", message)


# The prompt's own block tags: those of the score prompts' user turn and system blocks, and the single calls'.
BLOCK_NAMES: Final = (
    "job_ad",
    "candidate_profile",
    "draft",
    "required_facts",
    "validator_issues",
    "previous_draft",
    "issues",
    "cv_text",
    "search_locations",
)
_BLOCK_TAG: Final = re.compile(rf"<(\s*/?\s*(?:{'|'.join(BLOCK_NAMES)})(?:\s[^<>]*)?)>", re.IGNORECASE)


def neutralise_block_tags(text: str) -> str:
    """`text` with the `<` and `>` of every literal block tag of the prompt (`BLOCK_NAMES`) replaced by
    U+FF1C and U+FF1E (module docstring): the same length, the same offsets."""
    return _BLOCK_TAG.sub(
        lambda tag: f"\N{FULLWIDTH LESS-THAN SIGN}{tag[1]}\N{FULLWIDTH GREATER-THAN SIGN}", text
    )


def profile_document(profile: ConfirmedProfile) -> dict[str, JSONValue]:
    """The profile as the model reads it: its JSON document without `user_entered` (the model never needs the
    work authorisation or a clearance, and the profile holds no id or timestamp)."""
    return cast(dict[str, JSONValue], profile.model_dump(mode="json", exclude={"user_entered"}))


def candidate_profile_block(profile: ConfirmedProfile) -> str:
    """`<candidate_profile>` + the profile document (`profile_document`) as canonical JSON (sorted keys), its
    block tags neutralised, + `</candidate_profile>`; the same bytes for the same profile version (L3865)."""
    profile_json = canonical_json(profile_document(profile)).decode("utf-8")
    return f"<candidate_profile>{neutralise_block_tags(profile_json)}</candidate_profile>"


def propose_queries_prompt(profile: ConfirmedProfile, locations: Sequence[str]) -> RenderedPrompt:
    """The `propose_queries` request: the profile (`candidate_profile_block`), then the search locations as a
    JSON list inside `<search_locations>`."""
    locations_json = neutralise_block_tags(json.dumps(list(locations), ensure_ascii=False))
    text = f"{candidate_profile_block(profile)}\n<search_locations>{locations_json}</search_locations>"
    return _single_call("propose_queries", HumanMessage(text))


# ---- the prompt-schema versions ----------------------------------------------------------------------------


def prompt_schema_v1() -> tuple[dict[str, JSONValue], dict[str, JSONValue]]:
    """`(templates, schemas)` of prompt-schema version 1: each template of `TEMPLATE_NAMES_V1` by name, and
    each schema of `SCHEMAS_V1` transformed as a request sends it. Frozen: the M2 seed's hash."""
    templates: dict[str, JSONValue] = {name: template_text(name) for name in TEMPLATE_NAMES_V1}
    schemas: dict[str, JSONValue] = {name: transformed(model) for name, model in SCHEMAS_V1.items()}
    return templates, schemas


def prompt_schema_v2() -> tuple[dict[str, JSONValue], dict[str, JSONValue]]:
    """`(templates, schemas)` of prompt-schema version 2: the six templates of `TEMPLATE_NAMES_V2` (the two of
    version 1 byte-identical) and the four schemas of `SCHEMAS_V2` (the generator is candidate C)."""
    templates: dict[str, JSONValue] = {name: template_text(name) for name in TEMPLATE_NAMES_V2}
    schemas: dict[str, JSONValue] = {name: transformed(model) for name, model in SCHEMAS_V2.items()}
    return templates, schemas


def prompt_schema_hash(templates: Mapping[str, JSONValue], schemas: Mapping[str, JSONValue]) -> bytes:
    """`sha256(canonical_json(templates) ‖ canonical_json(schemas) ‖ RENDERER_VERSION)` (§7.3)."""
    return hashlib.sha256(
        canonical_json(dict(templates)) + canonical_json(dict(schemas)) + RENDERER_VERSION.encode("utf-8")
    ).digest()


@dataclass(frozen=True)
class PromptVersion:
    """A stored prompt-schema version as it renders: its id and hash, its templates and schemas by name, and
    the parser family its writer named. `load_prompt_version` hands each caller its own copy of the schemas
    (read-only at the top level), and `score_schema` a copy again, so no caller's change reaches another."""

    id: int
    prompt_schema_hash: bytes
    templates: Mapping[str, str]
    schemas: Mapping[str, JSONValue]
    parser_family: ParserFamily


class PromptVersionUnscorable(DomainError):
    """A version whose parser family cannot score: `generator_v1`, whose generator schema the API rejects (M2
    Deviation 33). The score subgraph ends the attempt `transient_error` without a charge. `version_id` is
    `None` where only the family is known (`scoring.validator.parse_generator`)."""

    def __init__(self, version_id: int | None, parser_family: str) -> None:
        which = "a prompt-schema version" if version_id is None else f"prompt-schema version {version_id}"
        super().__init__(f"{which} of family {parser_family} cannot score")
        self.version_id = version_id
        self.parser_family = parser_family


# The families a score prompt can be rendered for.
SCORABLE_FAMILIES: Final[frozenset[ParserFamily]] = frozenset({"generator_v2_items"})

_HASH_OF: Final = q("SELECT prompt_schema_hash FROM prompt_schema_versions WHERE id = :id")
_VERSION_ROW: Final = q(
    "SELECT templates, schemas, parser_family FROM prompt_schema_versions WHERE id = :id AND "
    "prompt_schema_hash = :hash"
)
# Per process, by `(id, hash)`: a version row is immutable, and a reused id with another hash is another key.
_LOADED: Final[dict[tuple[int, bytes], PromptVersion]] = {}


def _version(
    version_id: int, digest: bytes, templates: object, schemas: object, parser_family: object
) -> PromptVersion:
    """A loaded row, checked: its templates are texts, its family is known, and each schema is an object that
    `anthropic.transform_schema` leaves unchanged, so what a request sends (`gateway._chat` transforms it) is
    what `output_schema`, `count_body` and the byte bound read (`schemas.is_transform_fixed_point`)."""
    if not isinstance(templates, dict) or not all(isinstance(t, str) for t in templates.values()):
        raise ValueError(f"prompt-schema version {version_id}: its templates are no mapping of texts")
    if not isinstance(schemas, dict):
        raise ValueError(f"prompt-schema version {version_id}: its schemas are no mapping")
    for name, schema in schemas.items():
        if not isinstance(schema, dict) or not is_transform_fixed_point(schema):
            raise ValueError(
                f"prompt-schema version {version_id}: its {name!r} schema is not one a request sends as it "
                "is (no JSON-schema object, or one that anthropic.transform_schema changes)"
            )
    if parser_family not in PARSER_FAMILIES:
        raise ValueError(f"prompt-schema version {version_id}: an unknown parser family")
    return PromptVersion(
        id=version_id,
        prompt_schema_hash=digest,
        templates=MappingProxyType(dict(templates)),
        schemas=MappingProxyType(dict(schemas)),
        parser_family=parser_family,
    )


def _copy(version: PromptVersion) -> PromptVersion:
    """`version` with its own copy of every schema: what one caller does to a schema it was given never
    reaches the cache or another caller (the templates are texts)."""
    return replace(version, schemas=MappingProxyType(copy.deepcopy(dict(version.schemas))))


async def load_prompt_version(conn: AsyncConnection, version_id: int) -> PromptVersion:
    """The stored version `version_id` (its `templates`, `schemas` and `parser_family`), read once per process
    and `(id, hash)` and handed out as a copy; `LookupError` when there is none, `ValueError` for a row that
    fails `_version`'s checks. The schemas keep the key order the row stores (`schemas` is a `json` column,
    Ruling T4-a), which a structured output follows."""
    digest = (await conn.execute(_HASH_OF, {"id": version_id})).scalar_one_or_none()
    if digest is None:
        raise LookupError(f"no prompt-schema version {version_id}")
    key = (version_id, bytes(digest))
    if (cached := _LOADED.get(key)) is None:
        row = (await conn.execute(_VERSION_ROW, {"id": version_id, "hash": key[1]})).one_or_none()
        if row is None:
            raise LookupError(f"no prompt-schema version {version_id}")
        cached = _LOADED[key] = _version(version_id, key[1], row.templates, row.schemas, row.parser_family)
    return _copy(cached)


def _scorable(version: PromptVersion) -> None:
    if version.parser_family not in SCORABLE_FAMILIES:
        raise PromptVersionUnscorable(version.id, version.parser_family)


def _template(version: PromptVersion, name: str) -> str:
    text = version.templates.get(name)
    if text is None:
        raise ValueError(f"prompt-schema version {version.id} has no template {name!r}")
    return text


# ---- the <job_ad> block ------------------------------------------------------------------------------------

DESCRIPTION_HEAD: Final = 20_000
DESCRIPTION_TAIL: Final = 4_000
TRUNCATION_MARKER: Final = "[… truncated …]"
SegmentKind = Literal["structured_field", "description_head", "marker", "description_tail"]
_NOT_STATED: Final = "not stated"


@dataclass(frozen=True)
class Segment:
    """One part of a `<job_ad>` block's text, `text[block_start:block_end]` (module docstring)."""

    kind: SegmentKind
    field_name: str | None
    block_start: int
    block_end: int
    source_start: int | None


@dataclass(frozen=True)
class JobAdBlock:
    """The rendered `<job_ad>` block, whether its description was cut, and its origin map."""

    text: str
    truncated: bool
    segments: tuple[Segment, ...]


def _pairs(values: Sequence[tuple[str, str]]) -> str:
    return "; ".join(f"{value} ({source})" for value, source in values)


def job_ad_block(
    *,
    title: str,
    company: str | None,
    locations: Sequence[str],
    work_modes: Sequence[tuple[str, str]],
    employment_types: Sequence[tuple[str, str]],
    completeness: str,
    description_text: str,
) -> JobAdBlock:
    """The `<job_ad untrusted="true">` block of a job revision (§10.6 L3869-3872; module docstring), every
    field and the description with their block tags neutralised. `work_modes` and `employment_types` are
    `(value, source)` pairs."""
    fields = (
        ("title", "Title", title),
        ("company", "Company", company or ""),
        ("locations", "Locations", "; ".join(locations)),
        ("work_modes", "Work modes", _pairs(work_modes)),
        ("employment_types", "Employment types", _pairs(employment_types)),
        ("completeness", "Description completeness", completeness),
    )
    parts: list[tuple[SegmentKind, str | None, str, int | None]] = [
        ("marker", None, '<job_ad untrusted="true">\n', None),
        *(
            ("structured_field", name, f"{label}: {neutralise_block_tags(value) or _NOT_STATED}\n", None)
            for name, label, value in fields
        ),
        ("marker", None, "Description:\n", None),
    ]
    description_text = neutralise_block_tags(description_text)  # one code point for one: the same offsets
    size = len(description_text)
    truncated = size > DESCRIPTION_HEAD + DESCRIPTION_TAIL
    if truncated:
        parts += [
            ("description_head", None, description_text[:DESCRIPTION_HEAD], 0),
            ("marker", None, f"\n{TRUNCATION_MARKER}\n", None),
            ("description_tail", None, description_text[-DESCRIPTION_TAIL:], size - DESCRIPTION_TAIL),
        ]
    elif description_text:
        parts.append(("description_head", None, description_text, 0))
    parts.append(("marker", None, "\n</job_ad>", None))
    segments: list[Segment] = []
    position = 0
    for kind, field_name, text, source_start in parts:
        segments.append(Segment(kind, field_name, position, position + len(text), source_start))
        position += len(text)
    return JobAdBlock("".join(part[2] for part in parts), truncated, tuple(segments))


# ---- the shared rubric block -------------------------------------------------------------------------------

RUBRIC_BLOCK_MIN_TOKENS: Final = 600  # L3880, at chars/4
_PLACEHOLDER: Final = re.compile(r"\{\{([a-z_.]+)\}\}")
# The order of an anchor entry's keys: a rubric row read back from JSONB has lost the file's order.
_ANCHOR_KEYS: Final = (
    "criterion",
    "rule",
    "levels",
    "level_sub_rating",
    "years_sub_rating",
    "table",
    "recomputed",
    "examples",
    "unknown",
    "unconstrained",
    "unspecified_level",
)
_ANCHOR_LABELS: Final = {
    "criterion": "Rates",
    "level_sub_rating": "Level sub-rating",
    "years_sub_rating": "Years sub-rating",
}
_TABLE_COLUMNS: Final = ("level", "g", "y", "anchor", "sub_rating")


def estimated_tokens(text: str) -> int:
    """The conservative chars/4 estimate of `text`'s tokens (L3880)."""
    return len(text) // 4


def _ordered(keys: Sequence[str], preferred: Sequence[str]) -> list[str]:
    return [k for k in preferred if k in keys] + sorted(k for k in keys if k not in preferred)


def _cell(value: JSONValue) -> str:
    return str(value).replace("|", "\\|")


def _anchor_blocks(value: JSONValue) -> Iterator[str]:
    """The Markdown blocks of one anchor entry: a paragraph per text (labelled for `_ANCHOR_LABELS`), a table
    per list of rows (`_TABLE_COLUMNS` first), a bullet list per list of texts."""
    if isinstance(value, dict):
        for key in _ordered(list(value), _ANCHOR_KEYS):
            item = value[key]
            label = _ANCHOR_LABELS.get(key)
            if isinstance(item, str):
                yield f"{label}: {item}" if label else item
            else:
                if label:
                    yield f"{label}:"
                yield from _anchor_blocks(item)
    elif isinstance(value, list) and all(isinstance(row, dict) for row in value):
        rows = cast(list[dict[str, JSONValue]], value)
        columns = _ordered(sorted({key for row in rows for key in row}), _TABLE_COLUMNS)
        lines = ["| " + " | ".join(c.replace("_", " ") for c in columns) + " |", "|" + "---|" * len(columns)]
        lines += ["| " + " | ".join(_cell(row.get(c, "")) for c in columns) + " |" for row in rows]
        yield "\n".join(lines)
    elif isinstance(value, list):
        yield "\n".join(f"- {item}" for item in value)
    else:
        yield str(value)


def anchors_markdown(anchors: Mapping[str, JSONValue]) -> str:
    """A rubric's anchors as Markdown: `general` first, then the six LLM criteria in §11.2's order (then any
    other entry by name), each entry's keys in a fixed order, so the stored row and the packaged file render
    the same bytes."""
    sections = []
    for name in _ordered(list(anchors), ("general", *LLM_CRITERIA)):
        heading = "## General" if name == "general" else f"## `{name}`"
        sections.append("\n\n".join([heading, *_anchor_blocks(anchors[name])]))
    return "\n\n".join(sections)


def rubric_block(template: str, rubric: Rubric) -> str:
    """The shared rubric block: `template` (a version's `shared_rubric`) with `{{weight.<key>}}` replaced by
    the rubric's weight of that criterion and `{{anchors}}` by its anchors (`anchors_markdown`). An unknown
    placeholder is a `ValueError`."""
    values = {f"weight.{key}": str(weight) for key, weight in rubric.weights.items()}
    values["anchors"] = anchors_markdown(rubric.anchors)
    unknown = sorted({m.group(1) for m in _PLACEHOLDER.finditer(template)} - set(values))
    if unknown:
        raise ValueError(f"the shared rubric template names unknown placeholders: {', '.join(unknown)}")
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


# ---- the role blocks of the user turn ----------------------------------------------------------------------

_CODE: Final = re.compile(r"[a-z][a-z0-9_]*")
# A fact id `code:subject` (`scoring.facts`): a draft's fact-id code, then a subject of one line, no control
# character, at most 160 characters (a folded certification name of up to 120, or `other:<name>`).
_FACT_ID: Final = re.compile(rf"(?:{'|'.join(FACT_ID_CODES)}):[^\x00-\x1f\x7f]{{1,160}}")


@dataclass(frozen=True)
class Issue:
    """One issue as a score prompt states it in `<validator_issues>` or `<issues>`: a code, the criterion key
    or fact id it concerns, the quotes the validator already grounded, and a critic dispute's suggested level.
    Never free text: the code is an identifier, the target one of the six LLM criterion keys or a fact id
    `code:subject`, and no other text of the ad than those quotes."""

    code: str
    target: str | None = None
    quotes: tuple[str, ...] = ()
    suggested_level: int | None = None

    def __post_init__(self) -> None:
        if not _CODE.fullmatch(self.code):
            raise ValueError("an issue code is an identifier ([a-z][a-z0-9_]*)")
        if (
            self.target is not None
            and self.target not in LLM_CRITERIA
            and not _FACT_ID.fullmatch(self.target)
        ):
            raise ValueError("an issue target is an LLM criterion key or a fact id `code:subject`")
        if self.suggested_level is not None and self.suggested_level not in range(5):
            raise ValueError("a suggested level is 0 to 4")


def _json(value: JSONValue) -> str:
    return canonical_json(value).decode("utf-8")


def issues_block(issues: Sequence[Issue]) -> str:
    """The content of `<validator_issues>` or `<issues>`: the issues as a JSON list, in their order."""
    return _json(
        [
            {
                "code": issue.code,
                "target": issue.target,
                "quotes": list(issue.quotes),
                "suggested_level": issue.suggested_level,
            }
            for issue in issues
        ]
    )


def required_facts_block(refs: Sequence[FactRef]) -> str:
    """The content of `<required_facts>`: `fact_id`, `code` and `subject` of each fact the critic must
    review."""
    return _json([{"fact_id": r.fact_id, "code": r.code, "subject": r.subject} for r in refs])


def draft_block(draft: GeneratorDraft, fact_ids: Mapping[str, FactRef]) -> str:
    """The content of `<draft>` or `<previous_draft>`: the internal draft as canonical JSON, each binding fact
    whose id `fact_ids` holds carrying it as `fact_id` (the top-level flag as `not_a_job_ad_fact_id`), so the
    critic's targets are fact ids (§11.7.2). The first fact of an id carries it (L4174). A text fact id that
    names no fact of the draft is a `ValueError`; a structured one (a provider field) is in no draft."""
    doc = cast(dict[str, JSONValue], draft.model_dump(mode="json"))
    tagged: set[str] = set()

    def tag(node: Any, key: str, code: str, subject: str) -> None:
        name = fact_id(code, subject)
        if name in fact_ids and name not in tagged:
            node[key] = name
            tagged.add(name)

    for fact in binding_facts(draft.job_facts):  # the walk `derive_fact_ids` takes
        node: Any = doc["job_facts"]
        for step in fact.path:
            node = node[step]
        tag(node, "fact_id", fact.code, fact.subject)
    if draft.not_a_job_ad:
        tag(doc, "not_a_job_ad_fact_id", NOT_A_JOB_AD.code, NOT_A_JOB_AD.subject)
    if stray := sorted(k for k, ref in fact_ids.items() if ref.source == "text" and k not in tagged):
        raise ValueError(f"the fact ids {stray} name no fact of the draft")
    return _json(doc)


# ---- the score prompt --------------------------------------------------------------------------------------

ScoreRole = Literal["generate", "critic", "revise"]
# The tagged blocks of each role's user turn, in their order after the `<job_ad>` (L3873-3875).
_ROLE_BLOCKS: Final[Mapping[str, tuple[str, ...]]] = {
    "generate": (),
    "critic": ("draft", "required_facts", "validator_issues"),
    "revise": ("previous_draft", "issues"),
}


def score_schema(role: ScoreRole, version: PromptVersion) -> WireSchema:
    """The schema of a score node of `role` rendered from `version`: the stored `generator` schema with the
    mirror of the version's family for `generate` and `revise`, the stored `critic` schema with `CriticOut`
    for `critic`, each a copy of the version's. A version that cannot score raises
    `PromptVersionUnscorable`."""
    _scorable(version)
    name = "critic" if role == "critic" else "generator"
    stored = version.schemas.get(name)
    if not isinstance(stored, dict):
        raise ValueError(f"prompt-schema version {version.id} has no {name} schema")
    model: type[BaseModel] = CriticOut if role == "critic" else mirror_for(version.parser_family)
    return WireSchema(json=copy.deepcopy(stored), model=model)  # a copy: the node never shares the version's


def score_prompt(
    role: ScoreRole,
    *,
    version: PromptVersion,
    rubric: Rubric,
    profile: ConfirmedProfile,
    ad: JobAdBlock,
    draft: str | None = None,
    required_facts: str | None = None,
    validator_issues: str | None = None,
    previous_draft: str | None = None,
    issues: str | None = None,
) -> RenderedPrompt:
    """The request of a score node (module docstring). The critic takes exactly `draft`, `required_facts` and
    `validator_issues`, the reviser `previous_draft` and `issues` (each the block's content: `draft_block`,
    `required_facts_block`, `issues_block`), the generator none; anything else is a `ValueError`. A version
    that cannot score raises `PromptVersionUnscorable`."""
    _scorable(version)
    wanted = _ROLE_BLOCKS.get(role)
    if wanted is None:
        raise ValueError(f"no score role {role!r}")
    given = {
        "draft": draft,
        "required_facts": required_facts,
        "validator_issues": validator_issues,
        "previous_draft": previous_draft,
        "issues": issues,
    }
    if {name for name, block in given.items() if block is not None} != set(wanted):
        raise ValueError(f"the {role} prompt takes exactly the blocks {list(wanted)}")
    blocks = (f"\n<{name}>{neutralise_block_tags(given[name] or '')}</{name}>" for name in wanted)
    text = ad.text + "".join(blocks)
    return RenderedPrompt(
        system=(
            SystemBlock(rubric_block(_template(version, "shared_rubric"), rubric)),
            SystemBlock(candidate_profile_block(profile), cache=True),
            SystemBlock(_template(version, f"role_{role}")),
        ),
        messages=(HumanMessage(text),),
        template_version=f"{role}@{version.id}",
        input_refs={"prompt_schema_version_id": version.id, "truncated": ad.truncated},
    )
