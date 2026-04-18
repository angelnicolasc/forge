"""forge-os — meta-package that installs the full Forge stack.

Installing ``forge-os`` brings in ``forge-core``, ``forge-cli``,
``forge-observe``, ``forge-memory``, and ``forge-adapters`` together. Users
who want a granular API should import from those packages directly; this
module only exposes a couple of convenience re-exports so ``import forge_os``
works without surprises.
"""

from __future__ import annotations

__version__ = "0.1.0"

from forge_core.harness import MetaOrchestrator
from forge_core.types import RunConfig, TaskEnvelope

__all__ = ["MetaOrchestrator", "RunConfig", "TaskEnvelope", "__version__"]
