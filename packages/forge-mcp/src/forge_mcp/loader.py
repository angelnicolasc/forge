"""forge_mcp.loader — YAML configuration loader for forge-mcp server configs.

YAML schema (example):

    cache_ttl: 300
    servers:
      - name: filesystem
        transport: stdio
        command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        tools:
          read_file:
            allowed: true
            max_calls_per_run: 20
            redact_secrets: false
          write_file:
            allowed: true
            max_calls_per_run: 5

      - name: github
        transport: stdio
        command: ["npx", "-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_TOKEN: "${GITHUB_TOKEN}"
        tools:
          search_code:
            allowed: true
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from forge_mcp.types import ServerConfig, ToolPolicy

_ENV_RE = re.compile(r"\$\{(\w+)\}")


def _expand_env(value: str) -> str:
    """Replace ``${VAR}`` placeholders with environment variable values."""

    def _sub(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), m.group(0))

    return _ENV_RE.sub(_sub, value)


def _expand_env_in(obj: Any) -> Any:
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _expand_env_in(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_in(item) for item in obj]
    return obj


def load_yaml(source: str | Path | io.IOBase) -> tuple[list[ServerConfig], dict[str, Any]]:
    """Parse a forge-mcp YAML config file.

    Returns:
        (servers, options) where options may include ``cache_ttl``, etc.

    Raises:
        ValueError: malformed YAML or schema validation failure.
    """
    if isinstance(source, (str, Path)):
        path = Path(source)
        text = path.read_text(encoding="utf-8") if path.exists() else str(source)
    elif hasattr(source, "read"):
        raw = source.read()
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    else:
        raise TypeError(f"Expected str, Path, or file-like; got {type(source)}")

    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("forge-mcp config must be a YAML mapping at the top level.")

    data = _expand_env_in(data)

    raw_servers = data.get("servers", [])
    if not isinstance(raw_servers, list):
        raise ValueError("'servers' must be a list.")

    servers: list[ServerConfig] = []
    for i, raw in enumerate(raw_servers):
        try:
            servers.append(ServerConfig.model_validate(raw))
        except ValidationError as exc:
            raise ValueError(f"Server #{i} validation error:\n{exc}") from exc

    options: dict[str, Any] = {k: v for k, v in data.items() if k != "servers"}
    return servers, options


def dump_yaml(servers: list[ServerConfig], options: dict[str, Any] | None = None) -> str:
    """Serialize server configs back to canonical YAML."""
    data: dict[str, Any] = {}
    if options:
        data.update(options)
    data["servers"] = [s.model_dump(mode="json", exclude_none=True) for s in servers]
    return yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
