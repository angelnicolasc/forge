# Standalone preview release

The standalone wheel has a dedicated release path. A designated, disposable Vast Linux x86_64 VM builds the wheel and runs its acceptance. The GitHub intake verifies the resulting receipt and the signed source series. The publisher consumes that accepted artifact by digest.

## Create the Vast receipt

Use the repository's protected base and the maintainer's independently approved public SSH key to verify the complete candidate history. Supply a full Git checkout or the original signed objects reconstructed in a Git repository. The compatible baseline is the approved standalone 0.3.0 wheel.

```bash
python3 .github/standalone/verify_signatures.py --repo "$CHECKOUT" \
  --base "$STANDALONE_TRUST_BASE" --head "$CANDIDATE_SHA" \
  --allowed-signers "$ALLOWED_SIGNERS" --principal "$STANDALONE_SIGNER_PRINCIPAL" \
  --fingerprint "$STANDALONE_SIGNER_FINGERPRINT"
uv build "$CHECKOUT/standalone/forge-check" --no-sources --wheel --out-dir "$DIST"
```

Run the preview acceptance scripts against that exact wheel in the designated Vast reservation. Supply the expected baseline SHA-256 from the maintainer's approved artifact record. For the third command, install the wheel and its Code Mode extra from the approved wheelhouse in a clean Python 3.12 environment. Preserve the original reports and the Vast reservation and closure receipts outside the ZIP.

```bash
python3 tests/acceptance/preview_release.py --wheel "$WHEEL" --sha256 "$WHEEL_SHA256" \
  --previous "$BASELINE" --previous-sha256 "$BASELINE_SHA256" \
  --wheelhouse "$WHEELHOUSE" --output-dir "$P23_OUTPUT"
python3 tests/acceptance/preview_onboarding.py --wheel "$WHEEL" --out "$ONBOARDING_OUTPUT"
"$CODE_PYTHON" tests/acceptance/preview_bindings.py --wheel "$WHEEL" --output-dir "$BINDINGS_OUTPUT"
"$CODE_PYTHON" examples/governed_inventory.py
```

```bash
python3 .github/standalone/preview_artifact.py seal \
  --commit "$CANDIDATE_SHA" --wheel "$WHEEL" --wheel-sha256 "$WHEEL_SHA256" \
  --baseline-sha256 "$BASELINE_SHA256" --p23 "$P23_REPORT" \
  --onboarding "$ONBOARDING_REPORT" --bindings "$BINDINGS_REPORT" \
  --output "$OUT/standalone-preview.zip"
```

Retain the printed bundle and wheel digests in the operator's independent approval record. Use the same archive for intake. To rehearse staging offline on Vast, use `verify` with that bundle, both digests, the baseline digest, candidate SHA, Vast ID and a new `--stage` directory. Repeat with a wrong wheel digest and a wrong bundle digest. Each rejection must leave its staging directory absent.

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

[GitHub PR trigger security](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_target) · [Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
