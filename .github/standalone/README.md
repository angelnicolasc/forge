# Publishing the standalone wheel

Build the wheel once on a disposable Vast Linux x86_64 instance, run the release checks against that wheel, and publish those same bytes. GitHub verifies the signed commit series, the reports and artifact digests. For everyday CLI changes, the [public wheel tests](../../standalone/forge-check/tests/test_check_cli.py) run without a Vast instance.

## Create the Vast receipt

Use the repository's protected base and the maintainer's independently approved public SSH key to verify the complete candidate history. Supply a full Git checkout or the original signed objects reconstructed in a Git repository. The compatible baseline is the approved standalone 0.3.0 wheel.

```bash
python3 .github/standalone/verify_signatures.py --repo "$CHECKOUT" \
  --base "$STANDALONE_TRUST_BASE" --head "$CANDIDATE_SHA" \
  --allowed-signers "$ALLOWED_SIGNERS" --principal "$STANDALONE_SIGNER_PRINCIPAL" \
  --fingerprint "$STANDALONE_SIGNER_FINGERPRINT"
python3 .github/standalone/preview_artifact.py build --repo "$CHECKOUT" \
  --commit "$CANDIDATE_SHA" --dist "$DIST" --output "$OUT/build.json"
```

Record the instance ID from the Vast reservation in `FORGE_VAST_LAB_ID`. The scripts record this operator-supplied value. Intake compares it across the reports and with the workflow input. The operator checks the reservation and closure records separately and retains them outside the ZIP.

Run the checks against the candidate wheel. Use Python 3.12 for `preview_release.py` and supply the expected baseline SHA-256 from the maintainer's artifact record. Install the candidate with its Code Mode extra from the reviewed wheelhouse in a clean Python 3.12 environment for the commands that use `$CODE_PYTHON`. Preserve the original reports.

```bash
python3 tests/acceptance/preview_release.py --wheel "$WHEEL" --sha256 "$WHEEL_SHA256" \
  --previous "$BASELINE" --previous-sha256 "$BASELINE_SHA256" \
  --wheelhouse "$WHEELHOUSE" --output-dir "$P23_OUTPUT"
python3 tests/acceptance/preview_onboarding.py --wheel "$WHEEL" --out "$ONBOARDING_OUTPUT"
"$CODE_PYTHON" tests/acceptance/preview_bindings.py --wheel "$WHEEL" --output-dir "$BINDINGS_OUTPUT"
"$CODE_PYTHON" examples/governed_inventory.py
"$CODE_PYTHON" "$CHECKOUT/tests/acceptance/conditional_evidence.py" \
  --wheel "$WHEEL" --output-dir "$CONDITIONAL_OUTPUT"
"$CODE_PYTHON" "$CHECKOUT/tests/acceptance/conditional_gate_upgrade.py" \
  --previous-wheel "$RC2_WHEEL" --wheel "$WHEEL" --output "$OUT/gate-upgrade.json"
```

```bash
python3 .github/standalone/preview_artifact.py seal --repo "$CHECKOUT" \
  --commit "$CANDIDATE_SHA" --wheel "$WHEEL" --wheel-sha256 "$WHEEL_SHA256" \
  --baseline-sha256 "$BASELINE_SHA256" --p23 "$P23_REPORT" \
  --onboarding "$ONBOARDING_REPORT" --bindings "$BINDINGS_REPORT" \
  --build-report "$OUT/build.json" --conditional "$CONDITIONAL_OUTPUT/conditional-evidence.json" \
  --gate-upgrade "$OUT/gate-upgrade.json" \
  --output "$OUT/standalone-preview.zip"
```

From 0.4.0rc3 onward, build from the exact signed commit approved for promotion. The builder exports the package from Git objects into an isolated directory. Its receipt records the enclosing Git tree, package tree and wheel digest. Intake and publication look up both trees in the approved commit. They also require the conditional matrix for both routes, linked to the same wheel and Vast instance. RC1 and RC2 bundles retain their historical checks.

The dedicated Gate upgrade receipt exercises historical RC2 decisions, running canaries and shadows, and legacy scopes against the candidate on the same ledger. The P23 receipt independently covers the 0.3.0 packaging upgrade.

Retain the printed bundle and wheel digests in the operator's independent approval record. Use the same archive for intake. To rehearse staging offline on Vast, use `verify --repo "$CHECKOUT"` with that bundle, both digests, the baseline digest, candidate SHA, Vast ID and a new `--stage` directory. Repeat with a wrong wheel digest and a wrong bundle digest. Each rejection must leave its staging directory absent. For new bundles also attempt to seal a matrix missing one route and a build receipt from a different commit. Each failure must leave the requested ZIP path absent.

Run the publishing verifier on the valid ZIP and on deliberately altered reports with recomputed inner and outer digests. Each altered bundle must be rejected before the staging directory is created. Keep the output outside the bundle whose digest it verifies.

```bash
python3 "$CHECKOUT/tests/acceptance/preview_provenance.py" --repo "$CHECKOUT" \
  --bundle "$OUT/standalone-preview.zip" --bundle-sha256 "$BUNDLE_SHA256" \
  --commit "$CANDIDATE_SHA" --wheel-sha256 "$WHEEL_SHA256" \
  --baseline-sha256 "$BASELINE_SHA256" --vast-id "$FORGE_VAST_LAB_ID" \
  --output "$OUT/provenance-negative.json"
```

## Configure GitHub before use

The repository owner controls the trust inputs, environments, branch policy and publication approvals. Install the workflows on protected `main` under review before requiring them.

| Environment | Owner configuration |
|---|---|
| `standalone-signatures` | Restrict to the protected base. Set `STANDALONE_TRUST_BASE` to the independently approved full base commit. Set `STANDALONE_ALLOWED_SIGNER` to one allowed-signers record for the approved Ed25519 public key. Set `STANDALONE_SIGNER_PRINCIPAL` to the approved Git email and `STANDALONE_SIGNER_FINGERPRINT` to its expected OpenSSH SHA-256 fingerprint. |
| `standalone-preview-intake` | Restrict to protected `main` and require reviewer approval. Supply the same trust inputs, `STANDALONE_BASELINE_SHA256`, and the approved HTTPS hostname in `STANDALONE_VAST_BUNDLE_HOST`. Place a direct, short-lived read URL for the sealed archive in the environment secret `STANDALONE_VAST_BUNDLE_URL`. The URL must serve the ZIP bytes without a redirect. |
| `standalone-preview-testpypi` | Restrict to protected `main`, require reviewer approval and prevent self-review where supported. Supply the same baseline digest. On TestPyPI configure a Trusted Publisher for `angelnicolasc/forge`, workflow `standalone-preview-publish.yml`, environment `standalone-preview-testpypi`, project `forge-os-check`. |

Require the `forge/standalone-signed-series` status on the protected integration branch. The PR verifier uses the trusted base's script, fetches the candidate object, and reports the result on that SHA. The [Standalone wheel contract job](../workflows/ci.yml) exercises the installed CLI with read-only permissions. After observing the job on a pull request, add its status to branch protection alongside the signature status. Preserve the tested Git objects when advancing the branch and verify any new integration commit before merging it.

## Intake and promotion

1. Compare the Vast instance ID with the reservation and closure records. Set the short-lived read URL. Invoke **Standalone preview candidate** from `main` with `candidate_sha`, `vast_id`, `bundle_sha256` and `wheel_sha256`. The commit must be fetchable from the repository. Intake checks the signed source, the recorded IDs and both digests, then retains the ZIP and signature report as a run artifact.
2. Review the successful intake run and choose the attempt that produced the ZIP. Invoke **Publish standalone preview to TestPyPI** from `main` with that run ID, attempt and the same input values. The publisher checks the run identity, stages the matching release-candidate wheel and submits its bytes through OIDC.
3. Review the publication receipt. The workflow downloads the published wheel and compares its SHA-256 with the approved wheel. A post-publication failure requires investigation of the same release-candidate version. Keep the former approved artifact and use a new release-candidate version for changed bytes.

## Stable release

The stable workflows publish the wheel built in the lab and produce verification and publication receipts. Dispatch them from protected `main` using a reviewed commit reachable from that branch with a successful `forge/standalone-signed-series` status on its SHA. The workflows accept a commit SHA directly.

### Prepare the stable evidence in Vast

Build once from the approved commit and run the acceptance commands above against that wheel. Keep the 0.3.0 packaging receipt and the Gate upgrade from RC2. For the stable release, also demonstrate clean base and Code Mode installs on Linux x86_64 with both Python 3.11 and 3.12, and upgrade from the approved 0.4.0rc3 wheel on the same ledger.

```bash
"$PYTHON312" "$CHECKOUT/tests/acceptance/stable_compatibility.py" \
  --commit "$CANDIDATE_SHA" --wheel "$WHEEL" --wheel-sha256 "$WHEEL_SHA256" \
  --previous-wheel "$RC3_WHEEL" --wheelhouse "$WHEELHOUSE" \
  --python311 "$PYTHON311" --python312 "$PYTHON312" \
  --output "$OUT/stable-compatibility.json"
```

Add `--stable-compatibility "$OUT/stable-compatibility.json"` to the seal command. Rehearse staging with `verify --require-stable`. Confirm that a stable bundle missing conditional or upgrade evidence is rejected before staging.

### Configure the stable environments

Restrict each environment below to protected `main`. Configure required reviewers and prevent self-review on both publication environments. A reviewer other than the deployment initiator must approve each deployment under this configuration.

| Environment | Owner configuration |
|---|---|
| `standalone-stable-intake` | Use the same signing key, trusted ancestor, and 0.3.0 baseline as preview intake. Set `STANDALONE_VAST_BUNDLE_HOST` to the approved receipt origin and store the short-lived URL in `STANDALONE_VAST_BUNDLE_URL`. |
| `standalone-stable-testpypi` | On TestPyPI, register a Trusted Publisher for `angelnicolasc/forge`, workflow `standalone-stable-testpypi.yml`, environment `standalone-stable-testpypi`, project `forge-os-check`. |
| `standalone-stable-promotion` | Set `STANDALONE_BASELINE_SHA256` to the approved 0.3.0 digest and copy the approved signer and trust-base variables from `standalone-stable-intake`. This environment verifies the signed source, TestPyPI attempt, and exact wheel before staging. |
| `standalone-stable-pypi` | On PyPI, register a separate Trusted Publisher for `angelnicolasc/forge`, workflow `standalone-stable-pypi.yml`, environment `standalone-stable-pypi`, project `forge-os-check`. |

Review the sealed ZIP for public disclosure before running intake. The TestPyPI workflow retains that ZIP and its signature receipt as Actions artifacts. Keep raw lab logs and operational credentials outside the ZIP.

### Promote identical bytes

1. Invoke **Publish standalone stable to TestPyPI** with the approved SHA, Vast ID, bundle digest and wheel digest. Review the staged wheel and approve the TestPyPI deployment. Record the successful run ID and attempt, and preserve the receipt that ties the index download to the original bundle.
2. In a clean Vast consumer environment, download the exact version from TestPyPI and check its SHA-256 before installation. Install to a dedicated virtual environment. Record the first `forge check` verdict and explanation with the base install, the inventory demo with the Code Mode extra, and the approved rollback procedure. Record the production go/no-go against that run and evidence.
3. Invoke **Promote standalone stable to PyPI** with the same `candidate_sha`, `vast_id`, `bundle_sha256` and `wheel_sha256` values. Supply the TestPyPI run ID and attempt from step 1. The read-only job checks the sealed bundle and TestPyPI receipt, then downloads the staged wheel and checks its digest. The reviewer checks the consumer results from step 2 and decides whether to authorize the PyPI deployment.
4. Compare the published PyPI receipt with the approved wheel digest. Keep the separate TestPyPI and PyPI receipts with the Vast evidence. Investigate any post-upload failure before attempting the same version again.

## Recover an ambiguous upload

Before sending a wheel, the publisher checks the selected index. It uploads only when the version is absent. If the official index already has the exact, unyanked wheel, the workflow skips upload and checks the bytes again. A mismatched digest, unexpected distribution or failed index lookup stops the upload.

Every publication receipt records the preflight state, the upload step's raw outcome and the result of reading back the official index.

| Receipt state | Meaning |
|---|---|
| `trusted-upload-verified` | The version was absent, the Trusted Publishing action reported success, and the exact approved wheel was read back. |
| `recovered-index-verified` | The exact approved wheel is available. The upload step failed or was skipped because those bytes already existed. This receipt does not establish which publisher uploaded them. |
| `index-unverified` | The official read-back did not confirm the wheel. The workflow fails and retains its diagnostic receipt. |

Preserve the run ID, attempt, receipt and original evidence outside GitHub before the Actions artifacts expire. After a failure, inspect the official index for the approved version and digest before deciding whether to rerun. A stable rerun uses **Re-run all jobs** to regenerate the artifacts for that attempt. Keep `skip-existing: false`.

Production accepts a `trusted-upload-verified` TestPyPI receipt by default. For a `recovered-index-verified` receipt, the maintainer must record the recovery decision for that exact attempt, dispatch with `allow_recovered_testpypi: true` and approve the production environment. The production receipt preserves the TestPyPI state. The normal Vast consumer and rollback checks still apply.

[GitHub PR trigger security](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_target) · [Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
