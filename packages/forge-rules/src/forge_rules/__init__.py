"""forge_rules — Rules Engine for Forge.

Context-aware rule packs with fixed-lattice conflict resolution
and original scope intersection detection at lint time.
"""

from forge_rules.engine import RulesEngine
from forge_rules.loader import dump_yaml, load_yaml
from forge_rules.types import (
    IntraStrategy,
    Rule,
    RuleAction,
    RulePack,
    RuleSelection,
    ScopeIntersection,
)

__version__ = "0.1.0"

__all__ = [
    "IntraStrategy",
    "Rule",
    "RuleAction",
    "RulePack",
    "RuleSelection",
    "RulesEngine",
    "ScopeIntersection",
    "dump_yaml",
    "load_yaml",
]
