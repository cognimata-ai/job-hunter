"""Shared type aliases and typed accessors for JSON documents, and the strings a JSON document can hold that
PostgreSQL or UTF-8 cannot (`unstorable`, `json_strings`)."""

from collections.abc import Iterator
from typing import Final

type JSONValue = bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None


def json_get(doc: JSONValue, path: str) -> JSONValue:
    """The value at the dotted `path` through the objects of `doc`; an empty path is `doc` itself.

    A missing key raises `KeyError(path)`. A path that goes through a value that is not an object raises
    `TypeError(f"{prefix}: expected object, got {type name}")`, where `prefix` is the path up to that value.
    """
    node = doc
    if not path:
        return node
    parts = path.split(".")
    for depth, part in enumerate(parts):
        if not isinstance(node, dict):
            raise TypeError(_mismatch(".".join(parts[:depth]), "object", node))
        if part not in node:
            raise KeyError(path)
        node = node[part]
    return node


def _mismatch(path: str, kind: str, value: JSONValue) -> str:
    return f"{path or '<root>'}: expected {kind}, got {type(value).__name__}"


def get_object(doc: JSONValue, path: str) -> dict[str, JSONValue]:
    value = json_get(doc, path)
    if not isinstance(value, dict):
        raise TypeError(_mismatch(path, "object", value))
    return value


def get_list(doc: JSONValue, path: str) -> list[JSONValue]:
    value = json_get(doc, path)
    if not isinstance(value, list):
        raise TypeError(_mismatch(path, "list", value))
    return value


def get_str(doc: JSONValue, path: str) -> str:
    value = json_get(doc, path)
    if not isinstance(value, str):
        raise TypeError(_mismatch(path, "str", value))
    return value


def get_bool(doc: JSONValue, path: str) -> bool:
    value = json_get(doc, path)
    if not isinstance(value, bool):
        raise TypeError(_mismatch(path, "bool", value))
    return value


def get_int(doc: JSONValue, path: str) -> int:
    value = json_get(doc, path)
    # bool is a subclass of int: a JSON `true` is no integer here.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(_mismatch(path, "int", value))
    return value


NUL_MESSAGE: Final = "must not contain a NUL character"
SURROGATE_MESSAGE: Final = "must be valid Unicode text"


def unstorable(text: str) -> str | None:
    """Why `text` cannot be stored, or `None`: a NUL character (U+0000), which neither a `text` nor a `jsonb`
    value of PostgreSQL holds, or a lone UTF-16 surrogate, which a JSON escape (`"\\ud800"`) can carry and no
    UTF-8 encodes. The message never repeats the text."""
    if "\x00" in text:
        return NUL_MESSAGE
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return SURROGATE_MESSAGE
    return None


def json_strings(value: JSONValue, path: str) -> Iterator[tuple[str, str]]:
    """Every string value in `value` with its path from `path` (`.` between names, `[i]` after a list), in
    document order; keys are not visited."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from json_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from json_strings(item, f"{path}[{index}]")
