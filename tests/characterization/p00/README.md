# LangGraph compatibility in the 0.2.0 harness

This fixture checks whether a LangGraph run reaches a ticket system. Ticket `P00-S01` requires one HTTP GET. Ticket `P00-S02` requires none. Each route starts in a fresh process. The parent process runs a loopback HTTP server and records the requests it receives.

## Installation profiles

Use a fresh virtual environment for each profile and Python version. Save the installation commands, elapsed times, resolved versions and artifact hashes with the results.

| Profile | What to install |
| --- | --- |
| `public-wheel` | Install `forge-os[langgraph,review]==0.2.0` and its dependencies from PyPI. When installing PyTorch with `uv`, select the CPU backend. Record the source and SHA-256 of each Forge wheel. |
| `checkout` | Check out `7b8ea1e4a081cb41ead499029492e2ee5b286be3`. Install the local `forge-os`, `forge-os-core`, `forge-os-cli`, `forge-os-observe`, `forge-os-memory`, `forge-os-adapters` and `forge-review` packages in editable mode. Use the public profile's transitive dependency versions to isolate differences in Forge. |

For the original Vast setup, export `FORGE_VAST_LAB_ID` with the lease number from your inventory. The runner checks Linux and the numeric format, then records the supplied ID. Set `HARNESS` to the directory containing this file. Use a new output directory outside the checkout for each profile.

```bash
"$PUBLIC_ENV/bin/python" "$HARNESS/run.py" \
  --profile public-wheel --output "$EVIDENCE/public-wheel"
"$CHECKOUT_ENV/bin/python" "$HARNESS/run.py" \
  --profile checkout --checkout "$AUDITED_CHECKOUT" --output "$EVIDENCE/checkout"
```

## Reading the results

`p00.json` records the commands, exit codes and requests received by the ticket server. `environment.json` records installed dependencies and where Forge modules were imported from. Each route has its own stdout and stderr. The SDK also writes JSON. The historical `wrap` command prints text.

| Question | What the fixture checks |
| --- | --- |
| Does the documented `wrap` command make the same request as the SDK? | The fixture tries both argument orders with memory disabled and verbose output enabled. A missing `SOURCE` diagnostic (exit 2) and an undiscovered adapter (exit 1) are recorded separately. Neither failure sends a GET. A successful run must send the same GET as the native graph and explicit adapter. |
| Does removing a node change what the compiled graph runs? | `mutation.json` compares adapter topology before and after removing the lookup node. The fixture checks whether the server still receives a GET on the subsequent run. |
| Does the automatic mutation call the review hook? | `ReviewRunner.pre_apply` rejects removal of the required node when called directly. The fixture checks that `EvolutionLoop.step` applies the removal to the adapter topology without calling that hook. |

A successful characterization run can record a known CLI failure. Check each route's disposition in `p00.json` to see which entry points completed the lookup.
