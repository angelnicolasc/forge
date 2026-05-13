"""forge_mcp.security — Deny-by-default policy enforcement and secret redaction.

Security model:
  - Every tool is DENIED unless it appears in server.tools with allowed=True.
  - Caller allowlists narrow access further (empty list = any caller permitted).
  - Responses are scanned for common secret patterns and redacted before
    they reach the agent context window.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from forge_mcp.types import ServerConfig, ToolPolicy

# Common secret patterns — conservative, favour false-positives over false-negatives.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|bearer)\s*[:=]\s*\S+"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),  # OpenAI/Anthropic key
    re.compile(r"ghp_[a-zA-Z0-9]{20,}"),  # GitHub PAT
    re.compile(r"glpat-[a-zA-Z0-9\-_]{20,}"),  # GitLab PAT
    re.compile(r"(?:^|\s)Bearer\s+[A-Za-z0-9\-_.~+/]+=*", re.MULTILINE),  # Bearer token
    re.compile(r"eyJ[a-zA-Z0-9+/]{10,}={0,2}"),  # JWT (base64 header)
]


def get_policy(server: ServerConfig, tool_name: str) -> ToolPolicy | None:
    """Return the policy for *tool_name* on *server*, or None if not registered."""
    return server.tools.get(tool_name)


def is_allowed(server: ServerConfig, tool_name: str, caller_id: str = "") -> bool:
    """Deny-by-default check.

    Returns True iff:
      1. The tool is listed in server.tools.
      2. policy.allowed is True.
      3. If policy.allowed_callers is non-empty, caller_id is in the list.
    """
    policy = server.tools.get(tool_name)
    if policy is None or not policy.allowed:
        return False
    return not (policy.allowed_callers and caller_id not in policy.allowed_callers)


def redact_secrets(text: str) -> str:
    """Replace known secret patterns in *text* with ``[REDACTED]``."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_value(value: Any, policy: ToolPolicy) -> Any:
    """Recursively redact secrets in *value* if the policy requires it."""
    if not policy.redact_secrets:
        return value
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: redact_value(v, policy) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(item, policy) for item in value]
    return value
