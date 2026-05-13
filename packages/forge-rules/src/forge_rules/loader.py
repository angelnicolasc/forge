"""forge_rules.loader — YAML authoring layer for RulePacks.

YAML schema (example):

    name: python-safety
    version: "0.2.0"
    description: "Safety rules for Python codebases"
    intra_strategy: most_specific_wins
    rules:
      - id: no-eval
        action: deny
        description: "Prohibit eval() usage"
        content: "Never call eval() or exec() on untrusted input."
        scope:
          - "**/*.py"
        intent_tags: []
        priority: 0
        enabled: true
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from forge_rules.types import RulePack


def load_yaml(source: str | Path | io.IOBase) -> RulePack:
    """Parse a YAML rule pack from a string, file path, or file-like object.

    Raises:
        ValueError: if YAML is malformed or pack fails pydantic validation.
    """
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.exists():
            text = path.read_text(encoding="utf-8")
        else:
            # Treat as raw YAML string if path doesn't exist on disk
            text = str(source)
    elif hasattr(source, "read"):
        text = source.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8")
    else:
        raise TypeError(f"Expected str, Path, or file-like object; got {type(source)}")

    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Rule pack YAML must be a mapping at the top level.")

    try:
        return RulePack.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Rule pack validation error:\n{exc}") from exc


def dump_yaml(pack: RulePack) -> str:
    """Serialize a RulePack back to canonical YAML."""
    data = pack.model_dump(mode="json", exclude_none=True)
    return yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
