"""Forge Adapters — Drop-in wrappers for popular multi-agent frameworks.

Supported frameworks:
- LangGraph (langgraph_adapter)
- CrewAI (crewai_adapter)
- AutoGen (autogen_adapter)
- Any async callable (generic)
"""

from forge_adapters.base import BaseAdapter
from forge_adapters.discovery import discover_adapter
from forge_adapters.generic import GenericCallableAdapter

__all__ = ["BaseAdapter", "GenericCallableAdapter", "discover_adapter"]
