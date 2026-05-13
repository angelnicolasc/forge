"""forge_spec.loader — parse and serialize SpecDef artifacts.

Supports three source formats:
  str  → raw YAML content
  Path → read from disk
  IO   → any object with a .read() method

Artifact files:
  spec.yaml          — full spec definition (includes criteria + risks inline)
  acceptance.yaml    — acceptance criteria only (merged into SpecDef)
  risk_register.yaml — risk register only (merged into SpecDef)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from forge_spec.types import AcceptanceCriterion, Risk, SpecDef


def _read(source: str | Path | Any) -> tuple[str, str]:
    """Return (text, label) for any supported source type."""
    if isinstance(source, str):
        return source, "inline"
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8"), str(source)
    if hasattr(source, "read"):
        raw = source.read()
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        return text, "stream"
    raise TypeError(f"Unsupported source type: {type(source)!r}. Expected str, Path, or IO.")


def _parse_yaml(text: str, label: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error in {label!r}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"YAML must be a mapping at the top level, got {type(data).__name__}. Source: {label!r}"
        )
    return data


def load_spec_yaml(source: str | Path | Any) -> SpecDef:
    """Load a complete SpecDef from a spec.yaml (inline criteria + risks are supported)."""
    text, label = _read(source)
    data = _parse_yaml(text, label)
    try:
        return SpecDef.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Spec validation error in {label!r}:\n{exc}") from exc


def load_acceptance_yaml(source: str | Path | Any) -> list[AcceptanceCriterion]:
    """Load acceptance criteria from a standalone acceptance.yaml."""
    text, label = _read(source)
    data = _parse_yaml(text, label)
    raw = data.get("acceptance_criteria", data.get("criteria", []))
    if not isinstance(raw, list):
        raise ValueError(
            f"acceptance.yaml must contain a list under 'acceptance_criteria'. Source: {label!r}"
        )
    try:
        return [AcceptanceCriterion.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise ValueError(f"Acceptance criteria validation error in {label!r}:\n{exc}") from exc


def load_risk_register_yaml(source: str | Path | Any) -> list[Risk]:
    """Load risks from a standalone risk_register.yaml."""
    text, label = _read(source)
    data = _parse_yaml(text, label)
    raw = data.get("risks", data.get("risk_register", []))
    if not isinstance(raw, list):
        raise ValueError(f"risk_register.yaml must contain a list under 'risks'. Source: {label!r}")
    try:
        return [Risk.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise ValueError(f"Risk register validation error in {label!r}:\n{exc}") from exc


def merge_spec_files(
    spec_source: str | Path | Any,
    *,
    acceptance_source: str | Path | Any | None = None,
    risk_source: str | Path | Any | None = None,
) -> SpecDef:
    """Load a spec.yaml and optionally merge in separate acceptance + risk files.

    Separate files take precedence over inline definitions in spec.yaml.
    """
    spec = load_spec_yaml(spec_source)
    if acceptance_source is not None:
        criteria = load_acceptance_yaml(acceptance_source)
        spec = spec.model_copy(update={"acceptance_criteria": criteria})
    if risk_source is not None:
        risks = load_risk_register_yaml(risk_source)
        spec = spec.model_copy(update={"risks": risks})
    return spec


def dump_spec_yaml(spec: SpecDef) -> str:
    """Serialize a SpecDef to canonical YAML."""
    data = spec.model_dump(mode="json", exclude_none=True)
    return yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
