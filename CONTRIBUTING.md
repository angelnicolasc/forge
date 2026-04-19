# Contributing to Forge

Welcome! Forge is an open-core project and we love contributions.

## Development Setup

```bash
git clone https://github.com/angelnicolasc/forge.git
cd forge
pip install uv
uv sync --all-extras
uv run pre-commit install
```

## Running Tests

```bash
uv run pytest                          # all tests
uv run pytest packages/forge-core/    # single package
uv run pytest -k "test_evolution"     # by name
uv run pytest --cov=packages          # with coverage
```

## Code Style

```bash
uv run ruff check . --fix    # lint + autofix
uv run ruff format .         # format
uv run mypy packages/        # type check
```

All three must pass before opening a PR. Pre-commit hooks run them automatically.

## Project Structure

```
packages/
  forge-core/     # Core types, protocols, meta-orchestrator, evolution loop
  forge-memory/   # Living Collaborative Memory (vector + graph + symbolic)
  forge-adapters/ # Framework adapters (LangGraph, CrewAI, AutoGen)
  forge-observe/  # Telemetry, FinOps, REST + SSE API
  forge-cli/      # The `forge` command
  forge-os/       # Meta-package that installs the five above together
examples/         # Working end-to-end examples
docs/             # MkDocs documentation
tests/            # Cross-package E2E tests
```

## Adding a New Adapter

1. Create `packages/forge-adapters/src/forge_adapters/my_framework_adapter.py`
2. Inherit from `BaseAdapter` and implement `detect()`, `load()`, `run()`, `stream()`, `topology()`
3. Add to `forge_adapters/__init__.py`
4. Add detection logic to `discovery.py`
5. Register via entry point in `pyproject.toml`:
   ```toml
   [project.entry-points."forge.adapters"]
   my_framework = "forge_adapters.my_framework_adapter:MyFrameworkAdapter"
   ```
6. Add tests in `packages/forge-adapters/tests/`
7. Update [`docs/feature-map.md`](docs/feature-map.md) with the new adapter row

## Conventional Commits

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(memory): add Qdrant backend for production vector store
fix(cli): handle missing input file gracefully
docs(evolution): add self-evolution loop diagram
test(core): add property-based tests for RRF fusion
```

## Branch Protection

- `main` is protected. Open a PR and request review.
- All CI checks must pass.
- One approval required from a maintainer.

## License

By contributing, you agree your code is licensed under Apache-2.0.
