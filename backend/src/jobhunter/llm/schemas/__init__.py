"""The structured-output schemas (spec §11.5, §11.7.3, §12.4, §11.12): the structural Pydantic mirrors and
their JSON-schema export (`_base` holds the exporter).

- `json_schema(model)` is the spec's JSON schema of a mirror; a test pins each to the spec text.
- `transformed(model)` is `anthropic.transform_schema(json_schema(model))`: the `output_config.format.schema`
  a request of that schema sends, and so the schema part of its byte bound (§10.7) and of its fake-transport
  recording key.
- `SCHEMAS_V1` and `SCHEMAS_V2` name the four schemas of prompt-schema versions 1 and 2, as each stores them
  (`prompts/render.prompt_schema_v1` and `prompt_schema_v2`). Both are frozen constants that nothing adds to:
  version 2 differs in the generator only (`GeneratorWire`, candidate C), and a release that changes a schema
  ships a new version, never an edit of these.
- `PARSER_FAMILIES` names the parsers of a version's generator outputs, the member list of the
  `prompt_schema_versions.parser_family` CHECK (migration 0008, which writes it as a SQL literal). A version's
  family is the constant its writer stored, never derived from the schema (plan Deviation 40);
  `mirror_for(family)` is the structural mirror its outputs are validated against.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, cast

import anthropic
from pydantic import BaseModel

from jobhunter.llm.schemas._base import json_schema
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.generator import GeneratorOut, GeneratorWire
from jobhunter.llm.schemas.profile import ProfileOut
from jobhunter.llm.schemas.queries import QueryProposalOut
from jobhunter.types import JSONValue

__all__ = [
    "PARSER_FAMILIES",
    "SCHEMAS_V1",
    "SCHEMAS_V2",
    "ParserFamily",
    "json_schema",
    "mirror_for",
    "transformed",
]

SCHEMAS_V1: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {"generator": GeneratorOut, "critic": CriticOut, "profile": ProfileOut, "queries": QueryProposalOut}
)
SCHEMAS_V2: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {"generator": GeneratorWire, "critic": CriticOut, "profile": ProfileOut, "queries": QueryProposalOut}
)

ParserFamily = Literal["generator_v1", "generator_v2_items"]
PARSER_FAMILIES: Final[tuple[ParserFamily, ...]] = ("generator_v1", "generator_v2_items")
_MIRRORS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {"generator_v1": GeneratorOut, "generator_v2_items": GeneratorWire}
)


def mirror_for(family: ParserFamily) -> type[BaseModel]:
    """The structural mirror of the generator outputs of parser family `family` (`generator_v1`:
    `GeneratorOut`, `generator_v2_items`: `GeneratorWire`); another name is a `ValueError`."""
    mirror = _MIRRORS.get(family)
    if mirror is None:
        raise ValueError(f"no parser family {family!r}; known: {', '.join(PARSER_FAMILIES)}")
    return mirror


def transformed(model: type[BaseModel]) -> dict[str, JSONValue]:
    """The schema a request of `model` sends: `anthropic.transform_schema(json_schema(model))`."""
    return cast(dict[str, JSONValue], anthropic.transform_schema(json_schema(model)))
