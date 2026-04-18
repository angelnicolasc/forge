"""Forge configuration management.

Uses Pydantic Settings for environment variable + file-based config.
All config can be set via environment variables with FORGE_ prefix,
a ~/.forge/config.toml file, or programmatically.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class EvolutionConfig(BaseSettings):
    """Configuration for the self-evolution loop."""

    enabled: bool = False
    mode: str = "suggest"  # "suggest" | "auto" | "off"
    max_mutations_per_hour: int = 10
    cost_ceiling_per_eval: Decimal = Decimal("1.00")
    rollback_on_quality_drop: float = 0.1
    meta_agent_model: str = "claude-sonnet-4-20250514"
    min_runs_before_evolution: int = 3

    # β.4 — Auto-trigger knobs. Default OFF; flip the flag explicitly to
    # let MetaOrchestrator fire evolution.step() fire-and-forget after a
    # run completes. The debouncer fields prevent runaway mutation cycles
    # when runs fire in quick succession (e.g. during bulk evaluation).
    auto_trigger_enabled: bool = Field(default=False, alias="FORGE_ENABLE_EVOLUTION_AUTO")
    min_history_for_evolution: int = 2
    trigger_every_n_runs: int = 3
    min_interval_seconds: float = 60.0

    model_config = {"env_prefix": "FORGE_EVOLUTION_", "populate_by_name": True}


class MemoryConfig(BaseSettings):
    """Configuration for the Living Collaborative Memory."""

    enabled: bool = True
    vector_backend: str = "chromadb"
    graph_backend: str = "networkx"
    symbolic_enabled: bool = True
    persist_dir: Path = Path.home() / ".forge" / "memory"
    embedding_model: str = "all-MiniLM-L6-v2"
    vector_top_k: int = 10
    graph_max_hops: int = 3
    fusion_strategy: str = "reciprocal_rank"

    model_config = {"env_prefix": "FORGE_MEMORY_"}


class ObserveConfig(BaseSettings):
    """Configuration for observability and FinOps."""

    enabled: bool = True
    console_output: bool = True
    otlp_endpoint: str | None = None
    api_enabled: bool = False
    api_host: str = "127.0.0.1"
    api_port: int = 8787
    cost_alert_threshold: Decimal = Decimal("5.00")

    model_config = {"env_prefix": "FORGE_OBSERVE_"}


class ForgeConfig(BaseSettings):
    """Root configuration for the Forge harness."""

    project_name: str = "forge"
    log_level: str = "INFO"
    log_format: str = "console"  # "console" | "json"
    data_dir: Path = Path.home() / ".forge"

    # Sub-configs
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    observe: ObserveConfig = Field(default_factory=ObserveConfig)

    # Default run settings
    default_max_steps: int = 100
    default_timeout_seconds: float = 300.0
    default_cost_ceiling: Decimal = Decimal("10.00")

    model_config = {"env_prefix": "FORGE_"}

    def ensure_dirs(self) -> None:
        """Create necessary directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memory.persist_dir.mkdir(parents=True, exist_ok=True)
