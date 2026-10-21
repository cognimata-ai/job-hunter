"""The structured-output schemas (spec §11.5, §11.7.3, §12.4, §11.12): the structural Pydantic mirrors and
their JSON-schema export (`_base` holds the exporter).

- `json_schema(model)` is the spec's JSON schema of a mirror; a test pins each to the spec text.
- `transformed(model)` is `anthropic.transform_schema(json_schema(model))`: the `output_config.format.schema`
  a request of that schema sends, and so the schema part of its byte bound (§10.7) and of its fake-transport
  recording key.
- `SCHEMAS` names the four, as prompt-schema version 1 stores them (`prompts/render.prompt_schema_v1`).
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, cast

import anthropic
from pydantic import BaseModel

from jobhunter.llm.schemas._base import json_schema
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.generator import GeneratorOut
from jobhunter.llm.schemas.profile import ProfileOut
from jobhunter.llm.schemas.queries import QueryProposalOut
from jobhunter.types import JSONValue

__all__ = ["SCHEMAS", "json_schema", "transformed"]

SCHEMAS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {"generator": GeneratorOut, "critic": CriticOut, "profile": ProfileOut, "queries": QueryProposalOut}
)


def transformed(model: type[BaseModel]) -> dict[str, JSONValue]:
    """The schema a request of `model` sends: `anthropic.transform_schema(json_schema(model))`."""
    return cast(dict[str, JSONValue], anthropic.transform_schema(json_schema(model)))
