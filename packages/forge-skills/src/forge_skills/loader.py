"""forge_skills.loader — parse SkillDef from SKILL.md or YAML.

Source conventions (same as forge-mcp loader):
  str  → raw content (SKILL.md text or YAML string)
  Path → read from disk
  IO   → any object with a .read() method
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import yaml

from forge_skills.types import SkillDef, SkillParam

# Matches the YAML frontmatter block at the very start of a SKILL.md
_FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)


def _read_source(source: str | Path | Any) -> tuple[str, str]:
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


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from markdown body.

    Returns (meta_dict, body_text). If no frontmatter is found, returns ({}, text).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML frontmatter parse error: {exc}") from exc
    body = text[m.end():]
    return meta, body


def _build_skill(meta: dict[str, Any], *, body: str = "", source: str = "") -> SkillDef:
    """Construct a SkillDef from a raw metadata dict."""
    meta = dict(meta)
    raw_params = meta.pop("parameters", None) or []
    params: list[SkillParam] = []
    for p in raw_params:
        if isinstance(p, dict):
            params.append(SkillParam(**p))
        else:
            params.append(SkillParam(name=str(p)))
    return SkillDef(**{**meta, "parameters": params, "body": body.strip(), "source": source})


def load_skill_md(source: str | Path | Any) -> SkillDef:
    """Load a SkillDef from a SKILL.md document (YAML frontmatter + markdown body).

    Raises ValueError if the document has no YAML frontmatter.
    """
    text, label = _read_source(source)
    meta, body = _split_frontmatter(text)
    if not meta:
        raise ValueError(
            "SKILL.md must begin with a YAML frontmatter block (--- ... ---). "
            f"Source: {label!r}"
        )
    return _build_skill(meta, body=body, source=label)


def load_skill_yaml(source: str | Path | Any) -> SkillDef:
    """Load a SkillDef from a pure YAML string/file (no markdown body)."""
    text, label = _read_source(source)
    try:
        meta = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError(f"YAML must be a mapping, got {type(meta).__name__}. Source: {label!r}")
    return _build_skill(meta, source=label)


def load_dir(directory: str | Path, *, glob: str = "**/*.md") -> list[SkillDef]:
    """Scan *directory* for SKILL.md files and return all successfully parsed SkillDefs.

    Files that cannot be parsed (no frontmatter, invalid YAML, validation error) are
    silently skipped. Use load_skill_md() directly for strict error reporting.
    """
    root = Path(directory)
    skills: list[SkillDef] = []
    for path in sorted(root.glob(glob)):
        try:
            skills.append(load_skill_md(path))
        except Exception:
            pass
    return skills
