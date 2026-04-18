"""Auto-detection of multi-agent frameworks from user source code.

Inspects imports in the given Python file to determine which framework
is in use, then returns the appropriate adapter.
"""

from __future__ import annotations

import ast
from pathlib import Path

import structlog

from forge_core.errors import AdapterNotFoundError

logger = structlog.get_logger()

# Maps import patterns to adapter module paths
_FRAMEWORK_SIGNATURES: dict[str, str] = {
    "langgraph": "forge_adapters.langgraph_adapter",
    "crewai": "forge_adapters.crewai_adapter",
    "autogen": "forge_adapters.autogen_adapter",
    "autogen_agentchat": "forge_adapters.autogen_adapter",
}


def _extract_imports(source_path: Path) -> set[str]:
    """Extract all top-level import names from a Python file."""
    try:
        source_code = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source_code)
    except (SyntaxError, UnicodeDecodeError, FileNotFoundError) as exc:
        logger.warning("discovery.parse_failed", path=str(source_path), error=str(exc))
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])

    return imports


def detect_framework(source: str | Path) -> str | None:
    """Detect which multi-agent framework a source file uses.

    Returns the framework name (e.g., 'langgraph') or None if unknown.
    """
    path = Path(source)
    if not path.exists() or path.suffix != ".py":
        return None

    imports = _extract_imports(path)

    for framework, _adapter_module in _FRAMEWORK_SIGNATURES.items():
        if framework in imports:
            logger.info("discovery.detected", framework=framework, source=str(source))
            return framework

    return None


def discover_adapter(source: str | Path) -> object:
    """Auto-detect framework and return an instantiated adapter.

    Raises AdapterNotFoundError if no framework is detected.
    """
    import importlib

    framework = detect_framework(source)
    if framework is None:
        raise AdapterNotFoundError(str(source))

    module_path = _FRAMEWORK_SIGNATURES[framework]
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise AdapterNotFoundError(
            f"Framework '{framework}' detected but adapter not installed: {exc}"
        ) from exc

    # Convention: each adapter module exposes an Adapter class
    adapter_cls = getattr(module, "Adapter", None)
    if adapter_cls is None:
        raise AdapterNotFoundError(
            f"Adapter module '{module_path}' does not expose an 'Adapter' class."
        )

    return adapter_cls()
