"""Self-describing JSON Schema for the Gold-KG JSONL files (ARCHITECTURE.md §11.4).

``kg/schema.json`` ships beside the data so a downstream consumer can validate a
corpus without this codebase. Each ``*.jsonl`` file is a stream of one row type;
:data:`KG_SCHEMA` maps every file name to the JSON Schema (draft 2020-12) of one
of its rows.

To make "schema-valid JSONL" a real, dependency-free acceptance check we also
ship :func:`validate_row` / :func:`validate_rows`: a small validator covering the
exact subset of JSON Schema these schemas use (``type`` unions, ``required``,
``properties``, ``items``). It is deliberately narrow — enough to catch a missing
required key or a wrong scalar type, not a general-purpose validator.
"""

from __future__ import annotations

from typing import Any

__all__ = ["KG_SCHEMA", "SchemaError", "schema_document", "validate_row", "validate_rows"]

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

# -- reusable fragments ------------------------------------------------------

_PROPS = {"type": "object"}
_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}
_LOCATOR = {
    "type": "object",
    "properties": {
        "medium": _STR,
        "offset": {"type": "integer"},
        "length": {"type": "integer"},
        "line": {"type": "integer"},
    },
    "required": ["medium", "offset", "length", "line"],
}

#: file name -> JSON Schema of one row in that file.
KG_SCHEMA: dict[str, dict[str, Any]] = {
    "nodes.jsonl": {
        "type": "object",
        "properties": {
            "id": _STR,
            "type": _STR,
            "created_at": _STR,
            "props": _PROPS,
            "aliases": _STR_LIST,
        },
        "required": ["id", "type", "created_at", "props", "aliases"],
    },
    "edges.jsonl": {
        "type": "object",
        "properties": {
            "id": _STR,
            "type": _STR,
            "src": _STR,
            "dst": _STR,
            "created_at": _STR,
            "props": _PROPS,
        },
        "required": ["id", "type", "src", "dst", "created_at", "props"],
    },
    "events.jsonl": {
        "type": "object",
        "properties": {
            "id": _STR,
            "type": _STR,
            "timestamp": _STR,
            "actors": _PROPS,
            "initiative": {"type": ["string", "null"]},
            "project": {"type": ["string", "null"]},
            "subjects": _STR_LIST,
            "deliverable": {"type": ["object", "null"]},
            "parent_event": {"type": ["string", "null"]},
            "payload": _PROPS,
        },
        "required": ["id", "type", "timestamp", "actors", "subjects"],
    },
    "provenance.jsonl": {
        "type": "object",
        "properties": {
            "target_id": _STR,
            "artifacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"path": _STR},
                    "required": ["path"],
                },
            },
        },
        "required": ["target_id", "artifacts"],
    },
    "mentions.jsonl": {
        "type": "object",
        "properties": {
            "artifact_path": _STR,
            "entity_id": _STR,
            "surface_form": _STR,
            "locator": _LOCATOR,
        },
        "required": ["artifact_path", "entity_id", "surface_form", "locator"],
    },
    "aliases.jsonl": {
        "type": "object",
        "properties": {
            "entity_id": _STR,
            "canonical": _STR,
            "aliases": _STR_LIST,
        },
        "required": ["entity_id", "canonical", "aliases"],
    },
}


def schema_document() -> dict[str, Any]:
    """Return the full ``schema.json`` document (the per-file schema map)."""
    return {
        "$schema": SCHEMA_DIALECT,
        "title": "Enterprise-Sim Gold Knowledge Graph",
        "description": (
            "JSON Schema for the canonical kg/*.jsonl files. Each property is the "
            "schema of a single line (one row) of the like-named file."
        ),
        "files": dict(KG_SCHEMA),
    }


class SchemaError(ValueError):
    """Raised when a row fails validation against its declared schema."""


_TYPE_CHECKS: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _matches_type(value: Any, declared: Any) -> bool:
    """True when ``value`` matches a declared ``type`` (a name or list of names)."""
    names = declared if isinstance(declared, list) else [declared]
    for name in names:
        expected = _TYPE_CHECKS[name]
        # ``bool`` is an ``int`` subclass — keep them distinct so a flag is not an int.
        if name == "integer" and isinstance(value, bool):
            continue
        if isinstance(value, expected):
            return True
    return False


def _check(value: Any, schema: dict[str, Any], path: str) -> None:
    declared = schema.get("type")
    if declared is not None and not _matches_type(value, declared):
        raise SchemaError(f"{path}: expected type {declared!r}, got {type(value).__name__}")
    if declared == "object" or (isinstance(declared, list) and "object" in declared):
        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    raise SchemaError(f"{path}: missing required key {key!r}")
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    _check(value[key], sub, f"{path}.{key}")
    if declared == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _check(item, item_schema, f"{path}[{index}]")


def validate_row(file_name: str, row: dict[str, Any]) -> None:
    """Validate one row of ``file_name`` against :data:`KG_SCHEMA`; raise on failure."""
    try:
        schema = KG_SCHEMA[file_name]
    except KeyError:
        raise SchemaError(f"no schema for {file_name!r}") from None
    _check(row, schema, file_name)


def validate_rows(file_name: str, rows: list[dict[str, Any]]) -> None:
    """Validate every row of ``file_name``; raise :class:`SchemaError` on the first bad one."""
    for index, row in enumerate(rows):
        try:
            validate_row(file_name, row)
        except SchemaError as exc:
            raise SchemaError(f"{file_name} line {index + 1}: {exc}") from None
