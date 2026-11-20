"""Exercise the preview publishing gate against a sealed Vast candidate."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import zipfile
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib import error, request


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(document: dict) -> bytes:
    return (json.dumps(document, sort_keys=True) + "\n").encode()


def altered_bundle(original: dict, case: str, output: Path) -> str:
    members = dict(original)
    manifest = json.loads(members["receipt.json"])
    missing = {
        "missing_build": "build.json",
        "missing_conditional": "conditional.json",
        "missing_gate_upgrade": "gate-upgrade.json",
        "missing_stable_compatibility": "stable-compatibility.json",
    }.get(case)
    if missing is not None:
        del members[missing]
        del manifest["evidence_sha256"][missing]
    else:
        if case in {"build_commit", "package_tree"}:
            name = "build.json"
        elif case == "rc2_pending_canary":
            name = "gate-upgrade.json"
        elif case in {"missing_python311", "changed_live_history", "different_rc3"}:
            name = "stable-compatibility.json"
        else:
            name = "conditional.json"
        proof = json.loads(members[name])
        if case == "build_commit":
            proof["commit"] = "0" * 40
        elif case == "package_tree":
            proof["package_tree"] = "0" * 40
        elif case == "missing_route_case":
            proof["matrix"].pop(0)
        elif case == "incorrect_verdict":
            row = next(row for row in proof["matrix"] if row["scenario"] == "omitted")
            row["gate"]["report"]["verdict"] = "pass"
        elif case == "unsafe_alias":
            row = next(row for row in proof["matrix"] if row["scenario"] == "omitted")
            row["alias_after"]["generation"] += 1
        elif case == "observer_dispatch":
            proof["promotion_expiry"]["evaluation_dispatches"] = 1
        elif case == "lost_consistency_check":
            del proof["consistency"]["legacy_cache_without_expiry"]
        elif case == "different_wheel":
            proof["wheel_sha256"] = "0" * 64
        elif case == "rc2_pending_canary":
            proof["outcomes"]["canary"]["existing_root_provider_calls"] = 1
        elif case == "missing_python311":
            del proof["installs"]["3.11"]
        elif case == "changed_live_history":
            proof["upgrade"]["post_install_receipts_sha256"] = "0" * 64
        elif case == "different_rc3":
            proof["previous_wheel_sha256"] = "0" * 64
        else:
            raise ValueError(f"Unknown release probe {case}")
        members[name] = encode(proof)
        manifest["evidence_sha256"][name] = digest(members[name])
    members["receipt.json"] = encode(manifest)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return digest(output.read_bytes())


def index_probes(verifier: Path, manifest: dict, wheel: bytes) -> dict:
    main = runpy.run_path(str(verifier))["main"]
    version, filename = manifest["version"], manifest["wheel"]
    expected = digest(wheel)

    class Index:
        def __init__(self, responses: list[tuple[str, bytes | int]]) -> None:
            self.responses = deque(responses)

        def open(self, url: str, *, timeout: int) -> io.BytesIO:
            if not self.responses:
                raise RuntimeError(f"Unexpected index request: {url}")
            expected_url, response = self.responses.popleft()
            if url != expected_url:
                raise RuntimeError(f"Expected {expected_url}, got {url}")
            if isinstance(response, int):
                raise error.HTTPError(url, response, "synthetic response", {}, None)
            return io.BytesIO(response)

    def invoke(
        index: str, responses: list[tuple[str, bytes | int]], *, probe=True, release=version
    ):
        origin = Index(responses)
        argv = [
            str(verifier),
            "verify-index",
            "--index",
            index,
            "--version",
            release,
            "--wheel-sha256",
            expected,
        ]
        if probe:
            argv.append("--probe")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(request, "build_opener", return_value=origin),
            patch("time.sleep"),
            patch.object(sys, "argv", argv),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = main()
        if origin.responses:
            raise RuntimeError(f"Verifier did not complete its expected requests to {index}")
        output = stdout.getvalue()
        return status, json.loads(output) if output else None, stderr.getvalue()

    def rejects(index, responses, reason, **options):
        status, output, diagnostic = invoke(index, responses, **options)
        if status != 1 or output is not None or reason not in diagnostic:
            raise RuntimeError(f"Incorrect index rejection for {index}: {status} {diagnostic}")

    results = {}
    for index, metadata_host, file_host in (
        ("testpypi", "test.pypi.org", "test-files.pythonhosted.org"),
        ("pypi", "pypi.org", "files.pythonhosted.org"),
    ):
        metadata_url = f"https://{metadata_host}/pypi/forge-os-check/{version}/json"
        wheel_url = f"https://{file_host}/packages/{filename}"
        item = {
            "filename": filename,
            "url": wheel_url,
            "digests": {"sha256": expected},
            "yanked": False,
        }
        metadata = {"info": {"name": "forge-os-check", "version": version}, "urls": [item]}
        expected_receipt = {
            "index": index,
            "version": version,
            "wheel_sha256": expected,
            "bytes": len(wheel),
        }
        status, absent, diagnostic = invoke(index, [(metadata_url, 404)])
        if status != 0 or absent != {"index": index, "version": version, "state": "absent"}:
            raise RuntimeError(f"Index did not identify a release 404: {diagnostic}")
        live = [(metadata_url, encode(metadata)), (wheel_url, wheel)]
        status, present, diagnostic = invoke(index, live)
        if status != 0 or present != {**expected_receipt, "state": "verified"}:
            raise RuntimeError(f"Index did not verify the exact published bytes: {diagnostic}")

        # Read-back allows an index to start propagating after the uploader completes.
        delayed = [(metadata_url, 404), (metadata_url, encode({**metadata, "urls": []}))]
        status, receipt, diagnostic = invoke(index, [*delayed, *live], probe=False)
        if status != 0 or receipt != expected_receipt:
            raise RuntimeError(f"Read-back changed its retry behavior or receipt: {diagnostic}")

        cases = {
            "missing_wheel": (
                {**metadata, "urls": []},
                "Expected exactly one published wheel",
            ),
            "alternate_section": (
                {**metadata, "urls": [], "releases": {version: [item]}},
                "Expected exactly one published wheel",
            ),
            "alternate_distribution": (
                {**metadata, "urls": [{**item, "filename": "forge_os_check-0.4.0.tar.gz"}]},
                "Expected exactly one published wheel",
            ),
            "extra_distribution": (
                {**metadata, "urls": [item, {**item, "filename": "alternate.tar.gz"}]},
                "Index contains an unapproved distribution",
            ),
            "alternate_release": (
                {**metadata, "info": {**metadata["info"], "version": "0.4.0rc3"}},
                "Index metadata does not identify the approved release",
            ),
            "malformed_files": (
                {**metadata, "urls": "not an array"},
                "Index metadata must provide a list",
            ),
            "metadata_digest_mismatch": (
                {**metadata, "urls": [{**item, "digests": {"sha256": "0" * 64}}]},
                "Index reports a different wheel digest",
            ),
            "yanked_exact": (
                {**metadata, "urls": [{**item, "yanked": True}]},
                "Approved wheel is yanked",
            ),
            "untyped_yank_flag": (
                {**metadata, "urls": [{**item, "yanked": 0}]},
                "Approved wheel is yanked",
            ),
            "missing_yank_flag": (
                {
                    **metadata,
                    "urls": [{key: value for key, value in item.items() if key != "yanked"}],
                },
                "Approved wheel is yanked",
            ),
            "wrong_wheel_host": (
                {**metadata, "urls": [{**item, "url": f"https://example.invalid/{filename}"}]},
                "Unexpected wheel origin",
            ),
        }
        for document, reason in cases.values():
            rejects(index, [(metadata_url, encode(document))], reason)
        rejects(
            index,
            [(metadata_url, encode({**metadata, "urls": [{**item, "yanked": True}]}))],
            "Approved wheel is yanked",
            probe=False,
        )
        rejects(
            index,
            [(metadata_url, encode(metadata)), (wheel_url, wheel + b"altered")],
            "Published file bytes differ",
        )
        rejects(index, [(metadata_url, b"{")], "Preview artifact rejected")
        rejects(index, [(metadata_url, 503)], "Preview transfer failed")
        rejects(
            index, [(metadata_url, encode(metadata)), (wheel_url, 404)], "Preview transfer failed"
        )
        results[index] = {
            "absent": absent,
            "present": present,
            "read_back": receipt,
            "rejected": [
                *cases,
                "altered_bytes",
                "malformed_json",
                "metadata_service_unavailable",
                "wheel_download_404",
                "yanked_read_back",
            ],
        }

    rejects("pypi", [], "Production requires a final version", release="0.4.0rc3")
    results["production_prerelease_rejected"] = True
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "bundle", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    for name in ("commit", "wheel-sha256", "baseline-sha256", "vast-id", "bundle-sha256"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    if sys.platform != "linux" or os.environ.get("FORGE_VAST_LAB_ID") != args.vast_id:
        parser.error("Run on the identified Vast instance")
    verifier = Path(__file__).resolve().parents[2] / ".github/standalone/preview_artifact.py"
    command = [
        sys.executable,
        str(verifier),
        "verify",
        "--repo",
        str(args.repo),
        "--commit",
        args.commit,
        "--wheel-sha256",
        args.wheel_sha256,
        "--baseline-sha256",
        args.baseline_sha256,
        "--vast-id",
        args.vast_id,
    ]
    expected_errors = {
        "missing_conditional": "unexpected entries",
        "build_commit": "Build receipt must identify",
        "package_tree": "Build receipt must identify",
        "missing_route_case": "Missing required conditional cases",
        "incorrect_verdict": "Incorrect conditional verdict",
        "unsafe_alias": "must preserve the alias",
        "observer_dispatch": "Expired evidence must leave the alias unchanged",
        "lost_consistency_check": "Missing or incorrect receipt consistency result",
        "different_wheel": "describes a different candidate",
        "rc2_pending_canary": "Gate upgrade failed the canary compatibility policy",
    }
    with zipfile.ZipFile(args.bundle) as archive:
        original = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(original["receipt.json"])
    version = manifest["version"]
    final = bool(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version))
    if final:
        expected_errors.update(
            missing_build="unexpected entries",
            missing_gate_upgrade="unexpected entries",
            missing_stable_compatibility="unexpected entries",
            missing_python311="Require Python 3.11 and 3.12 installs",
            changed_live_history="must preserve the live RC3 ledger",
            different_rc3="must identify this commit, wheel, host and the approved RC3 wheel",
        )
    channel = "--require-stable" if final else "--require-preview"
    wrong_channel = "--require-preview" if final else "--require-stable"

    with tempfile.TemporaryDirectory(prefix="forge-provenance-") as temporary:
        root = Path(temporary)

        def verify(
            bundle: Path, sha256: str, stage: Path, mode: str
        ) -> subprocess.CompletedProcess:
            return subprocess.run(
                [
                    *command,
                    mode,
                    "--bundle",
                    str(bundle),
                    "--bundle-sha256",
                    sha256,
                    "--stage",
                    str(stage),
                ],
                capture_output=True,
                text=True,
            )

        stage = root / "positive"
        valid = verify(args.bundle, args.bundle_sha256, stage, channel)
        if valid.returncode != 0:
            raise RuntimeError(valid.stderr)
        (wheel,) = stage.iterdir()
        if digest(wheel.read_bytes()) != args.wheel_sha256:
            raise RuntimeError("Positive staging produced different wheel bytes")
        wrong_stage = root / "wrong-channel"
        wrong = verify(args.bundle, args.bundle_sha256, wrong_stage, wrong_channel)
        if (
            wrong.returncode != 1
            or "release channel requires" not in wrong.stderr
            or wrong_stage.exists()
        ):
            raise RuntimeError(f"Invalid channel reached staging: {wrong.stderr}")
        outcomes = {"wrong_channel": {"exit": wrong.returncode, "staged": False}}
        for case, expected in expected_errors.items():
            altered = root / f"{case}.zip"
            altered_sha = altered_bundle(original, case, altered)
            stage = root / case
            result = verify(altered, altered_sha, stage, channel)
            if result.returncode != 1 or expected not in result.stderr or stage.exists():
                raise RuntimeError(f"Unexpected decision for {case}: {result.stderr}")
            outcomes[case] = {"exit": result.returncode, "reason": expected, "staged": False}
    receipt = {
        "status": "pass",
        "version": version,
        "commit": args.commit,
        "wheel_sha256": args.wheel_sha256,
        "bundle_sha256": args.bundle_sha256,
        "vast_lab_id": args.vast_id,
        "positive_stage_sha256": args.wheel_sha256,
        "negative_probes": outcomes,
    }
    if final:
        receipt["index_policy_probes"] = index_probes(
            verifier, manifest, original[manifest["wheel"]]
        )
    with args.output.open("xb") as output:
        output.write(encode(receipt))


if __name__ == "__main__":
    main()
