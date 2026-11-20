# Standalone preview release

The standalone wheel has a dedicated release path. A designated, disposable Vast Linux x86_64 VM builds the wheel and runs its acceptance. The GitHub intake verifies the resulting receipt and the signed source series. The publisher consumes that accepted artifact by digest.

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

Run the preview acceptance scripts against that exact wheel in the designated Vast reservation. Supply the expected baseline SHA-256 from the maintainer's approved artifact record. For the third command, install the wheel and its Code Mode extra from the approved wheelhouse in a clean Python 3.12 environment. Preserve the original reports and the Vast reservation and closure receipts outside the ZIP.

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

Require the `forge/standalone-signed-series` status from GitHub Actions on the protected integration branch. Preserve the verified Git objects when advancing the branch and verify any new merge commit under the same policy. Keep write credentials unavailable to untrusted pull-request workflows. The PR verifier uses the trusted base's script, fetches the exact candidate object, and reports the status on the candidate SHA.

## Intake and promotion

1. Check the Vast closure and approval record. Set the short-lived read URL and invoke **Standalone preview candidate** from `main` with the approved commit, Vast ID, bundle digest and wheel digest. The candidate commit must be fetchable from the repository at this point. Intake verifies these inputs and retains the sealed ZIP and signature report as a run artifact.
2. Review the successful intake run and exact attempt. Invoke **Publish standalone preview to TestPyPI** from `main` with the approved run ID, attempt and the same four inputs. The publisher checks the run identity, stages one verified release-candidate wheel and submits the bytes through OIDC.
3. Review the publication receipt. The workflow downloads the published wheel and compares its SHA-256 with the approved wheel. A post-publication failure requires investigation of the same release-candidate version. Keep the former approved artifact and use a new release-candidate version for changed bytes.

## Stable release

The stable lane publishes final versions of the standalone wheel. Use the reviewed commit reachable from protected `main`, with a successful `forge/standalone-signed-series` status on that exact SHA. The stable workflows dispatch only from `main`. They build no artifacts and require no release tag.

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

Restrict every environment below to protected `main`. Keep independent approvals on both publication environments and enable prevention of self-review.

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
3. Invoke **Promote standalone stable to PyPI** with the same four approvals and the approved TestPyPI run ID and attempt. The read-only job re-verifies the sealed bundle, checks the TestPyPI receipt, and downloads and hashes the wheel again from TestPyPI. Keep the PyPI deployment approval pending until the reviewer has checked the Vast consumer evidence and recorded the go/no-go.
4. Compare the published PyPI receipt with the approved wheel digest. Keep the separate TestPyPI and PyPI receipts with the Vast evidence. Investigate any post-upload failure before attempting the same version again.

[GitHub PR trigger security](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_target) · [Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
