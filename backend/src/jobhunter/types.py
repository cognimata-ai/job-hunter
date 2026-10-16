"""Shared type aliases and typed accessors for JSON documents."""

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
