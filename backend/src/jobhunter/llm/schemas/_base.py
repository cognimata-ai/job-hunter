"""The structural mirror base and its JSON-schema export (spec §11.5, §11.6 stage 1).

A `SchemaModel` mirrors a structured-output schema exactly: types, enums and required keys, no extra keys
(`extra="forbid"`), strict types (a JSON `"7"` is no number and `1` no boolean), and **no** length or count
limit; those are the semantic validators' (§11.6, §12.4).

`json_schema(model)` exports a mirror in the conservative subset the spec allows: `type`, `enum`, `anyOf`,
`$ref`/`$defs`, `required`, `additionalProperties: false`, and the profile's `format: date`. The exporter is
small on purpose, so that every keyword it emits is one the spec text has:
- `str`, `bool`, `int`, `float` and `None` are `string`, `boolean`, `integer`, `number` and `null`;
- `Literal[...]` is its type with an `enum` (strings or integers);
- `list[X]` is an `array` of `X`, and `X | Y` an `anyOf` in that order;
- a nested mirror is an inline object with all its fields required;
- `Annotated[X, Def(name)]` is `{"$ref": "#/$defs/<name>"}`, with `X` in the top-level `$defs`;
- `Annotated[str, Format(name)]` is a `string` with that `format`.
Any other annotation, a limit included, is a `TypeError`. A field with a default is left out of `required`
(only `ConfirmedProfile.user_entered` has one, and it is no structured output).

`SchemaModel.model_json_schema()` returns that schema, so `anthropic.transform_schema(model)`, which
langchain's `with_structured_output(…, method="json_schema")` and the gateway's byte bound both call, sends
the spec's schema and not pydantic's own (titles, class-named `$defs`).
"""

import types
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, BeforeValidator, ConfigDict
from pydantic.json_schema import DEFAULT_REF_TEMPLATE, GenerateJsonSchema, JsonSchemaMode

from jobhunter.types import JSONValue


@dataclass(frozen=True)
class Def:
    """`Annotated[X, Def(name)]`: `X` is the schema's `$defs/<name>`, referenced with `$ref`."""

    name: str


@dataclass(frozen=True)
class Format:
    """`Annotated[str, Format(name)]`: a string with the JSON-schema `format` `name`."""

    name: str


def _not_bool(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("a boolean is not an integer")  # pydantic reports a ValueError as a validation error
    return value


# A strict integer enum: pydantic's `Literal[0, 1]` also takes `True` and `False` (they equal 1 and 0).
NOT_BOOL = BeforeValidator(_not_bool)


class SchemaModel(BaseModel):
    """The base of every structured-output mirror (see the module docstring)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    @classmethod
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = DEFAULT_REF_TEMPLATE,
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        mode: JsonSchemaMode = "validation",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> dict[str, Any]:
        """The spec's JSON schema (`json_schema(cls)`); the mirrors have no aliases, serialisers or other
        references, so pydantic's generator options do not apply and any other value is refused."""
        defaults = (True, DEFAULT_REF_TEMPLATE, GenerateJsonSchema, "validation", "any_of")
        if (by_alias, ref_template, schema_generator, mode, union_format) != defaults:
            raise TypeError(f"{cls.__name__} exports the spec's JSON schema only, with pydantic's defaults")
        return json_schema(cls)


def json_schema(model: type[BaseModel]) -> dict[str, JSONValue]:
    """The JSON schema of the mirror `model`, with the `$defs` it references at the top level."""
    defs: dict[str, JSONValue] = {}
    schema = _object(model, defs)
    if defs:
        schema["$defs"] = defs
    return schema


def _object(model: type[BaseModel], defs: dict[str, JSONValue]) -> dict[str, JSONValue]:
    if model.model_config.get("extra") != "forbid":
        raise TypeError(f"{model.__name__} must forbid extra keys (additionalProperties: false)")
    properties: dict[str, JSONValue] = {}
    required: list[JSONValue] = []
    for name, field in model.model_fields.items():
        if field.alias not in (None, name):
            raise TypeError(f"{model.__name__}.{name}: aliases are not mirrored")
        # The annotation with the `Annotated` metadata pydantic moved into the field (`Def`, `Format`).
        properties[name] = _node(field.rebuild_annotation(), defs)
        if field.is_required():
            required.append(name)
    return {"type": "object", "additionalProperties": False, "required": required, "properties": properties}


def _node(annotation: Any, defs: dict[str, JSONValue]) -> dict[str, JSONValue]:
    metadata: tuple[object, ...] = ()
    if get_origin(annotation) is Annotated:
        annotation, *rest = get_args(annotation)
        metadata = tuple(rest)
    if unknown := [m for m in metadata if not isinstance(m, Def | Format) and m is not NOT_BOOL]:
        raise TypeError(f"outside the structured-output subset (limits are the validator's): {unknown!r}")
    node = _plain(annotation, defs)
    for marker in metadata:
        if isinstance(marker, Format):
            if node != {"type": "string"}:
                raise TypeError(f"format {marker.name!r} applies to a plain string only")
            node = {**node, "format": marker.name}
    for marker in metadata:
        if isinstance(marker, Def):
            if defs.setdefault(marker.name, node) != node:
                raise TypeError(f"two different definitions of $defs/{marker.name}")
            return {"$ref": f"#/$defs/{marker.name}"}
    return node


def _plain(annotation: Any, defs: dict[str, JSONValue]) -> dict[str, JSONValue]:
    scalar = {str: "string", bool: "boolean", int: "integer", float: "number", type(None): "null"}
    if isinstance(annotation, type) and annotation in scalar:
        return {"type": scalar[annotation]}
    origin = get_origin(annotation)
    if origin is Literal:
        values = list(get_args(annotation))
        if all(isinstance(v, str) for v in values):
            return {"type": "string", "enum": values}
        if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            return {"type": "integer", "enum": values}
        raise TypeError(f"an enum of strings or of integers only: {annotation!r}")
    if origin is list:
        (item,) = get_args(annotation)
        return {"type": "array", "items": _node(item, defs)}
    if origin is Union or origin is types.UnionType:
        return {"anyOf": [_node(option, defs) for option in get_args(annotation)]}
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _object(annotation, defs)
    raise TypeError(f"outside the structured-output subset: {annotation!r}")
