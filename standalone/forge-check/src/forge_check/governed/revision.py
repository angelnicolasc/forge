"""Validated immutable manifest for explicit governed bindings."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, NoReturn, cast

LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2
VERIFIER = "forge-check/1"
CONDITIONAL_VERIFIER = "forge-check/2"
CONDITIONAL_CERTIFICATE = "forge-check/2.1"
MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_DOCUMENT_BYTES = 1_048_576
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def certificate_verifier(scope_verifier: str) -> str:
    """Keep manifest compatibility separate from the semantics that authorized a decision."""
    return CONDITIONAL_CERTIFICATE if scope_verifier == CONDITIONAL_VERIFIER else scope_verifier


def identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"Invalid {label} identifier")
    return value


def digest_id(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("Invalid revision digest")
    return value


def integer(value: object, label: str, *, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Invalid {label} integer")
    return value


def _string(value: object, label: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid {label} string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"Invalid UTF-8 in {label}") from exc
    if size > limit:
        raise ValueError(f"Oversized {label} string")
    return value


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    return cast("dict[str, Any]", value)


def _shape(value: object, label: str, required: set[str], optional: set[str]) -> dict[str, Any]:
    obj = _object(value, label)
    if required - obj.keys() or obj.keys() - required - optional:
        raise ValueError(f"Missing or unsupported fields in {label}")
    return obj


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON object key")
        obj[key] = value
    return obj


def _unsupported_number(_value: str) -> NoReturn:
    raise ValueError("Only integers in the safe JSON range are supported")


def read_json(raw: str | bytes) -> dict[str, Any]:
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise ValueError("JSON document exceeds the size limit")
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return _object(
            json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_float=_unsupported_number,
                parse_constant=_unsupported_number,
            ),
            "Document",
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Malformed UTF-8 JSON document") from exc


def _ordered(value: object) -> object:
    # RFC 8785 property order is UTF-16 code-unit order. The restricted profile
    # uses JSON integers in the interoperable range and excludes all floats.
    if value is None or isinstance(value, (bool, str)):
        return value
    if type(value) is int and -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        return value
    if isinstance(value, list):
        return [_ordered(item) for item in value]
    if isinstance(value, dict):
        obj = _object(value, "Canonical value")
        return {key: _ordered(obj[key]) for key in sorted(obj, key=lambda s: s.encode("utf-16be"))}
    raise ValueError(
        "Canonical JSON accepts only strings, booleans, safe integers, null and containers"
    )


def canonical_json(value: object) -> bytes:
    try:
        result = json.dumps(_ordered(value), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Value cannot be canonicalized") from exc
    if len(result) > _MAX_DOCUMENT_BYTES:
        raise ValueError("Canonical JSON exceeds the size limit")
    return result


def document_digest(domain: str, canonical: bytes) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + canonical).hexdigest()


def string_set(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    ids = [identifier(item, label) for item in value]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate identifier in {label}")
    return sorted(ids)


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"Invalid {label} boolean")
    return value


def _binding(value: object, schema_version: int) -> dict[str, Any]:
    item = _object(value, "Binding")
    common = {"kind", "version", "contract", "tariff", "reserve_microusd"}
    options = {"idempotency", "cache_ttl_seconds"}
    kind = item.get("kind")
    if kind == "model":
        _shape(
            item,
            "model binding",
            common | {"provider", "model", "prompt"},
            options
            | {
                "max_output_tokens",
                "parameters",
                "snapshot",
            },
        )
    elif kind == "tool":
        _shape(item, "tool binding", common | {"server", "method", "effect"}, options)
    else:
        raise ValueError("Unsupported binding kind")

    contract = _object(item["contract"], "Binding contract")
    if schema_version == SCHEMA_VERSION:
        from forge_check.governed.binding_contract import normalize_contract

        contract = normalize_contract(contract)
    reserve = item["reserve_microusd"]
    result: dict[str, Any] = {
        "kind": kind,
        "version": identifier(item["version"], "binding version"),
        "contract": contract,
        "tariff": identifier(item["tariff"], "tariff"),
        "reserve_microusd": None if reserve is None else integer(reserve, "reservation"),
        "idempotency": _boolean(item.get("idempotency", False), "idempotency"),
        "cache_ttl_seconds": integer(item.get("cache_ttl_seconds", 0), "cache TTL", maximum=3600),
    }
    if kind == "model":
        if result["cache_ttl_seconds"]:
            raise ValueError("Only read tools may be cached")
        result.update(
            provider=identifier(item["provider"], "provider"),
            model=identifier(item["model"], "model"),
            prompt=_string(item["prompt"], "prompt", limit=65_536),
            parameters=_object(item.get("parameters", {}), "Model parameters"),
            snapshot=_boolean(item.get("snapshot", False), "model snapshot declaration"),
            max_output_tokens=integer(
                item.get("max_output_tokens", 256), "max output tokens", minimum=1, maximum=65_536
            ),
        )
    else:
        effect = item["effect"]
        if effect not in ("read", "write"):
            raise ValueError("Unsupported tool effect")
        if result["cache_ttl_seconds"] and effect != "read":
            raise ValueError("Only read tools may be cached")
        result.update(
            server=identifier(item["server"], "tool server"),
            method=identifier(item["method"], "tool method"),
            effect=effect,
        )
    return result


@dataclass(frozen=True, slots=True, init=False)
class Revision:
    canonical: bytes
    digest: str

    @classmethod
    def parse(cls, document: object) -> Revision:
        root = _shape(
            document,
            "Revision",
            {
                "schema_version",
                "scope",
                "budget_microusd",
                "policy",
                "check",
                "bindings",
            },
            set(),
        )
        version = root["schema_version"]
        if type(version) is not int or version not in (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION):
            raise ValueError("Unsupported revision schema version")
        bindings = {
            identifier(key, "binding"): _binding(value, version)
            for key, value in _object(root["bindings"], "Binding catalog").items()
        }
        if not bindings:
            raise ValueError("A revision requires at least one binding")
        kinds = sorted(spec["kind"] for spec in bindings.values())
        if version == LEGACY_SCHEMA_VERSION and kinds != ["model", "tool"]:
            raise ValueError("Revision schema 1 requires one model and one tool binding")
        policy = _shape(root["policy"], "Admission policy", {"allowed_bindings"}, set())
        allowed = string_set(policy["allowed_bindings"], "allowed bindings")
        if set(allowed) - bindings.keys():
            raise ValueError("Admission policy names an unknown binding")
        check = _shape(root["check"], "Release check", {"rule_id", "verifier"}, set())
        if check["verifier"] not in (VERIFIER, CONDITIONAL_VERIFIER):
            raise ValueError("Unsupported release verifier")

        canonical = canonical_json(
            {
                "schema_version": version,
                "scope": identifier(root["scope"], "scope"),
                "budget_microusd": integer(root["budget_microusd"], "root budget"),
                "policy": {"allowed_bindings": allowed},
                "check": {
                    "rule_id": identifier(check["rule_id"], "rule"),
                    "verifier": check["verifier"],
                },
                "bindings": bindings,
            }
        )
        rev = object.__new__(cls)
        object.__setattr__(rev, "canonical", canonical)
        object.__setattr__(rev, "digest", document_digest(f"forge.revision.v{version}", canonical))
        return rev

    @classmethod
    def from_json(cls, raw: str | bytes) -> Revision:
        return cls.parse(read_json(raw))

    @classmethod
    def from_canonical(cls, data: bytes) -> Revision:
        revision = cls.from_json(data)
        if revision.canonical != data:
            raise ValueError("Non-canonical stored revision")
        return revision

    @property
    def document(self) -> dict[str, Any]:
        return read_json(self.canonical)


def catalog_digest(revision: Revision) -> str:
    """Pin the parts implemented by the approved host driver."""
    catalog = {}
    for binding, spec in revision.document["bindings"].items():
        common = ("kind", "version", "contract", "idempotency")
        target = ("provider",) if spec["kind"] == "model" else ("server", "method", "effect")
        catalog[binding] = {field: spec[field] for field in common + target}
    return document_digest("forge.catalog.v1", canonical_json(catalog))
