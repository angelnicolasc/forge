# Earlier Forge harness — test reference

These source tests cover the earlier agent harness under `packages/`. Each link opens the test for the named behavior. For the standalone Forge Check product, see [Check saved agent results in CI](check.md).

| Harness behavior | Test |
| --- | --- |
| The generic adapter restores the patched SDK method after an exception. | [`test_generic_adapter_restores_sdk_after_exception`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-adapters/tests/test_generic_adapter.py#L242) |
| The CrewAI fixture publishes tool events to the harness bus. | [`test_execute_publishes_tool_events`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-adapters/tests/test_crewai_adapter.py#L236) |
| The AutoGen adapter converts a paired start and end in its structured log into an LLM event. | [`test_handler_paired_llm_start_and_end_publishes_llm_call_event`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-adapters/tests/test_autogen_adapter.py#L118) |
| The harness aggregates recorded costs by agent and model. | [`test_cost_aggregated_by_agent_and_model`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-core/tests/test_e2e_cost_tracking.py#L95) |
| Reported cost above the ceiling cancels the test run and raises a budget error. | [`test_run_with_guards_raises_budget_exceeded_when_cost_trips`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-core/tests/test_guards.py#L117) |
| The local graph store preserves entries across a restart. | [`test_graph_persistence_survives_restart`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-memory/tests/test_graph_persistence.py#L45) |
| A run submitted through the observe API appears in the run list. | [`test_run_ingest_and_list`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-observe/tests/test_api.py#L60) |
| The MCP budget raises an error when the call allowance is exhausted. | [`test_exceeds_limit_raises`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-mcp/tests/test_budget.py#L20) |
| Rule selection filters by the supplied path. | [`test_scope_filter`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-rules/tests/test_engine.py#L125) |
| The skills dispatcher rejects a match below its confidence threshold. | [`test_resolve_below_threshold_raises`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-skills/tests/test_dispatcher.py#L72) |
| The spec report counts passed, failed and skipped criteria. | [`test_attest_criterion_counts`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-spec/tests/test_attester.py#L71) |
| The review cascade preserves the severity of a heuristic finding. | [`test_heuristic_findings_returned`](https://github.com/angelnicolasc/forge/blob/9216000d01ea08c32f8bc38db80d11ddb46f1865/packages/forge-review/tests/test_cascade.py#L43) |

The [LangGraph characterization](https://github.com/angelnicolasc/forge/blob/main/tests/characterization/p00/README.md) exercises native, adapter and CLI paths separately. It documents a CLI argument-order discrepancy and a difference between reported topology and independently observed execution.

The spec report test passes with the runtime type import in this source tree. The published `forge-spec==0.2.0` wheel and Pydantic 2.11.10 can fail while constructing `SpecDef` because the wheel imports `MutationKind` only during type checking.
