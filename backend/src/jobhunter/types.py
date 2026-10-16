"""Shared type aliases."""

type JSONValue = bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None
