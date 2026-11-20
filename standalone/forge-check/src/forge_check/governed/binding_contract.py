"""The bounded operation schema used by the host and its agent interfaces."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from forge_check.governed.revision import (
    LEGACY_SCHEMA_VERSION,
    MAX_SAFE_INTEGER,
    SCHEMA_VERSION,
    identifier,
    integer,
    string_set,
)

_SCHEMA_VERSION = 1
_MAX_DEPTH = 12
_MAX_PROPERTIES = 64
_MAX_ARRAY_ITEMS = 256
_MAX_STRING_LENGTH = 20_000
_SCALAR_TYPES = ("string", "integer", "boolean", "null")


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Expected a JSON object")
    return cast("dict[str, Any]", value)


def _keys(schema: dict[str, Any], required: set[str], optional: set[str]) -> None:
    if required - schema.keys() or schema.keys() - required - optional - {"type"}:
        raise ValueError("Missing or unsupported operation schema keyword")


def _range(
    schema: dict[str, Any], low: str, high: str, *, floor: int, ceiling: int
) -> tuple[int, int]:
    minimum = integer(schema.get(low, floor), low, minimum=floor, maximum=ceiling)
    maximum = integer(schema.get(high, ceiling), high, minimum=minimum, maximum=ceiling)
    return minimum, maximum


def _type(value: object) -> tuple[str, bool]:
    allowed = (*_SCALAR_TYPES, "object", "array")
    if isinstance(value, str) and value in allowed:
        return value, False
    if isinstance(value, list) and len(value) == 2 and "null" in value:
        for entry in value:
            if isinstance(entry, str) and entry in allowed and entry != "null":
                return entry, True
    raise ValueError("Unsupported operation schema type")


def _normalize(value: object, depth: int) -> dict[str, Any]:
    if depth > _MAX_DEPTH:
        raise ValueError("Operation schema is too deeply nested")
    schema = _object(value)
    kind, nullable = _type(schema.get("type"))
    normalized: dict[str, Any] = {"type": [kind, "null"] if nullable else kind}

    if kind == "object":
        _keys(schema, {"properties"}, {"required", "additionalProperties"})
        properties = _object(schema["properties"])
        if (
            len(properties) > _MAX_PROPERTIES
            or schema.get("additionalProperties", False) is not False
        ):
            raise ValueError("Operation objects require a bounded, closed property catalog")
        required = string_set(schema.get("required", []), "required properties")
        if set(required) - properties.keys():
            raise ValueError("Required operation property is undeclared")
        normalized.update(
            properties={
                identifier(name, "property"): _normalize(child, depth + 1)
                for name, child in properties.items()
            },
            required=required,
            additionalProperties=False,
        )
    elif kind == "array":
        _keys(schema, {"items"}, {"minItems", "maxItems"})
        minimum, maximum = _range(schema, "minItems", "maxItems", floor=0, ceiling=_MAX_ARRAY_ITEMS)
        normalized.update(
            items=_normalize(schema["items"], depth + 1), minItems=minimum, maxItems=maximum
        )
    elif kind == "string":
        _keys(schema, set(), {"minLength", "maxLength", "format"})
        minimum, maximum = _range(
            schema, "minLength", "maxLength", floor=0, ceiling=_MAX_STRING_LENGTH
        )
        normalized.update(minLength=minimum, maxLength=maximum)
        if "format" in schema:
            if schema["format"] != "identifier":
                raise ValueError("Unsupported operation string format")
            normalized["format"] = "identifier"
    elif kind == "integer":
        _keys(schema, set(), {"minimum", "maximum"})
        minimum, maximum = _range(
            schema, "minimum", "maximum", floor=-MAX_SAFE_INTEGER, ceiling=MAX_SAFE_INTEGER
        )
        normalized.update(minimum=minimum, maximum=maximum)
    else:
        _keys(schema, set(), set())
    return normalized


def normalize_schema(schema: object) -> dict[str, Any]:
    """Normalize a closed schema for an operation argument or result object."""
    normalized = _normalize(schema, 0)
    if normalized["type"] != "object":
        raise ValueError("Operation input and output schemas must be objects")
    return normalized


def _validated(value: object, schema: dict[str, Any], *, project: bool) -> object:
    kind, nullable = _type(schema["type"])
    if value is None and (nullable or kind == "null"):
        return None
    if kind == "object":
        instance = _object(value)
        properties = schema["properties"]
        if set(schema["required"]) - instance.keys() or (
            not project and instance.keys() - properties.keys()
        ):
            raise ValueError("Operation object instance disagrees with its property catalog")
        return {
            name: _validated(instance[name], child, project=project)
            for name, child in properties.items()
            if name in instance
        }
    if kind == "array" and isinstance(value, list):
        if schema["minItems"] <= len(value) <= schema["maxItems"]:
            return [_validated(item, schema["items"], project=project) for item in value]
    elif kind == "string" and isinstance(value, str):
        if schema["minLength"] <= len(value) <= schema["maxLength"]:
            value.encode("utf-8")  # Reject unpaired surrogate escapes before transport.
            if schema.get("format") == "identifier":
                identifier(value, "operation string")
            return value
    elif kind == "integer" and type(value) is int:
        if schema["minimum"] <= value <= schema["maximum"]:
            return value
    elif kind == "boolean" and type(value) is bool:
        return value
    raise ValueError("Operation value violates its schema")


def validate_instance(value: object, schema: dict[str, Any]) -> None:
    """Validate a JSON object against a previously normalized schema."""
    _validated(value, schema, project=False)


def normalize_contract(value: object) -> dict[str, Any]:
    contract = _object(value)
    if set(contract) != {"schema_version", "input", "output", "visible"}:
        raise ValueError("Missing or unsupported operation contract fields")
    version = contract["schema_version"]
    if type(version) is not int or version != _SCHEMA_VERSION:
        raise ValueError("Unsupported operation contract version")
    output = normalize_schema(contract["output"])
    visible = string_set(contract["visible"], "visible output properties")
    if set(visible) - output["properties"].keys():
        raise ValueError("Visible operation property is undeclared")
    return {
        "schema_version": version,
        "input": normalize_schema(contract["input"]),
        "output": output,
        "visible": visible,
    }


@dataclass(frozen=True)
class OperationContract:
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    legacy: bool

    def validate_arguments(self, payload: object) -> None:
        validate_instance(payload, self.input_schema)

    def project(self, result: object) -> dict[str, object]:
        return _object(_validated(result, self.output_schema, project=True))


def _legacy_contract(model: bool) -> OperationContract:
    properties: dict[str, Any] = {"ticket_id": {"type": "string", "format": "identifier"}}
    if model:
        properties["text"] = {"type": "string", "minLength": 1, "maxLength": _MAX_STRING_LENGTH}
    return OperationContract(
        normalize_schema(
            {"type": "object", "properties": properties, "required": list(properties)}
        ),
        normalize_schema(
            {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string", "format": "identifier"},
                    "label": {"type": "string", "format": "identifier"},
                },
                "required": ["ticket_id", "label"],
            }
        ),
        legacy=True,
    )


def operation_contract(revision_schema: int, binding: dict[str, Any]) -> OperationContract:
    """Expose the pinned schema and its visible result projection."""
    if revision_schema == LEGACY_SCHEMA_VERSION:
        return _legacy_contract(binding["kind"] == "model")
    if revision_schema != SCHEMA_VERSION:
        raise ValueError("Unsupported revision schema version")
    contract = binding["contract"]
    output = deepcopy(contract["output"])
    visible = set(contract["visible"])
    output["properties"] = {
        name: spec for name, spec in output["properties"].items() if name in visible
    }
    output["required"] = [name for name in output["required"] if name in visible]
    return OperationContract(deepcopy(contract["input"]), output, legacy=False)
