"""forge-core test package — fixture path wiring.

Under pytest's ``importlib`` import mode (set at the workspace root so
the five package test dirs don't collide on the shared ``tests``
module name) relative imports like ``from .fixtures.mock_llm_adapter``
fail because test files are not imported inside a package hierarchy.
Injecting this directory onto ``sys.path`` lets those tests keep their
``from fixtures.mock_llm_adapter import ...`` form without every file
having to learn a new import path.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
