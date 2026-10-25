"""The M2 prompt renderer (spec §10.6, §9.5, §5.5.1) and prompt-schema version 1 (§7.3; plan Deviation 17).

Each request is one call with no cache marker (§10.6: profile extraction and query proposal are single
calls): one static system block, the template as it is packaged, and one user turn that holds the input.
Nothing dynamic (a date, an id, the input) is ever in the system block.

- `extract_profile_prompt(reviewed_text)` puts the reviewed text inside `<cv_text>…</cv_text>` exactly, so
  the string inside the tags is the text whose sha256 the draft holds (`transmitted_text_sha256`, §9.5).
- `extract_profile_native_pdf_prompt(pdf)` sends the file as a native `document` block (§12.7), with the
  same template.
- `propose_queries_prompt(profile, locations)` sends the confirmed profile as canonical JSON without its
  `user_entered` block (the model never needs the work authorisation or a clearance) and the search locations
  as a JSON list.

`input_refs` is empty here: the nodes add the ids they own (Tasks 13 and 20). `template_version(name)` is
`<name>@<the first 12 hex digits of the template's sha256>`, recorded on every call.

**Prompt-schema version 1** (`prompt_schema_v1()`) is the two templates, by name, and the four transformed
schemas (`schemas.SCHEMAS`). Its `prompt_schema_hash` is `sha256(canonical_json(templates) ‖
canonical_json(schemas) ‖ RENDERER_VERSION)`; `RENDERER_VERSION` names the framing this module adds around
the templates (the tags and the native-PDF instruction), so a change to it is a new version.
"""

import base64
import hashlib
import json
from collections.abc import Sequence
from importlib import resources
from typing import Final, Literal, get_args

from langchain_core.messages import HumanMessage

from jobhunter.db.seed import canonical_json
from jobhunter.llm.gateway import RenderedPrompt, SystemBlock
from jobhunter.llm.schemas import SCHEMAS, transformed
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.types import JSONValue

RENDERER_VERSION: Final = "1"

TemplateName = Literal["profile_extract", "propose_queries"]
TEMPLATE_NAMES: Final[tuple[TemplateName, ...]] = get_args(TemplateName)

# The user turn's text after a native PDF document block (§12.7).
NATIVE_PDF_INSTRUCTION: Final = "The CV is the attached PDF document. Extract the candidate profile from it."


def template_text(name: TemplateName) -> str:
    """The packaged template `name` (`<name>.md`), exactly as stored."""
    if name not in TEMPLATE_NAMES:
        raise ValueError(f"no prompt template {name!r}")
    return resources.files("jobhunter.llm.prompts").joinpath(f"{name}.md").read_text("utf-8")


def template_version(name: TemplateName) -> str:
    """`<name>@<sha256(template)[:12]>`: the `llm_calls.prompt_template_version` of a call."""
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


def propose_queries_prompt(profile: ConfirmedProfile, locations: Sequence[str]) -> RenderedPrompt:
    """The `propose_queries` request: the profile as canonical JSON (sorted keys, no `user_entered`) inside
    `<candidate_profile>`, then the search locations as a JSON list inside `<search_locations>`."""
    profile_json = canonical_json(profile.model_dump(mode="json", exclude={"user_entered"})).decode("utf-8")
    locations_json = json.dumps(list(locations), ensure_ascii=False)
    text = (
        f"<candidate_profile>{profile_json}</candidate_profile>\n"
        f"<search_locations>{locations_json}</search_locations>"
    )
    return _single_call("propose_queries", HumanMessage(text))


def prompt_schema_v1() -> tuple[dict[str, JSONValue], dict[str, JSONValue]]:
    """`(templates, schemas)` of prompt-schema version 1: each template's text by name, and each schema of
    `SCHEMAS` transformed as a request sends it."""
    templates: dict[str, JSONValue] = {name: template_text(name) for name in TEMPLATE_NAMES}
    schemas: dict[str, JSONValue] = {name: transformed(model) for name, model in SCHEMAS.items()}
    return templates, schemas


def prompt_schema_hash(templates: dict[str, JSONValue], schemas: dict[str, JSONValue]) -> bytes:
    """`sha256(canonical_json(templates) ‖ canonical_json(schemas) ‖ RENDERER_VERSION)` (§7.3)."""
    return hashlib.sha256(
        canonical_json(templates) + canonical_json(schemas) + RENDERER_VERSION.encode("utf-8")
    ).digest()
