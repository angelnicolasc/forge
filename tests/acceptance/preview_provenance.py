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
from pathlib import Path
from unittest.mock import patch
from urllib import request


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
    check_index = runpy.run_path(str(verifier))["check_index"]
    version, filename = manifest["version"], manifest["wheel"]
    expected = digest(wheel)

    class Index:
        def __init__(self, responses: dict[str, bytes]) -> None:
            self.responses = responses
            self.requests: list[str] = []

        def open(self, url: str, *, timeout: int) -> io.BytesIO:
            self.requests.append(url)
            return io.BytesIO(self.responses[url])

    fixtures = {}
    for index, metadata_host, file_host in (
        ("testpypi", "test.pypi.org", "test-files.pythonhosted.org"),
        ("pypi", "pypi.org", "files.pythonhosted.org"),
    ):
        metadata_url = f"https://{metadata_host}/pypi/forge-os-check/{version}/json"
        wheel_url = f"https://{file_host}/packages/{filename}"
        metadata = {
            "info": {"name": "forge-os-check", "version": version},
            "urls": [{"filename": filename, "url": wheel_url, "digests": {"sha256": expected}}],
        }
        fixtures[index] = (metadata_url, wheel_url, metadata)

    results = {}
    for index, (metadata_url, wheel_url, metadata) in fixtures.items():
        args = argparse.Namespace(index=index, version=version, wheel_sha256=expected)
        origin = Index({metadata_url: encode(metadata), wheel_url: wheel})
        with patch.object(request, "build_opener", return_value=origin):
            approval = check_index(args)
        if (
            approval["index"] != index
            or approval["wheel_sha256"] != expected
            or origin.requests != [metadata_url, wheel_url]
        ):
            raise RuntimeError(f"Verifier did not read the approved bytes from {index}")
        results[index] = approval

    def rejects(args: argparse.Namespace, index: Index, reason: str) -> None:
        with patch.object(request, "build_opener", return_value=index):
            try:
                check_index(args)
            except ValueError as error:
                if reason in str(error):
                    return
                raise RuntimeError(f"Wrong index rejection: {error}") from error
        raise RuntimeError("Production verification approved unexpected index evidence")

    production = argparse.Namespace(index="pypi", version=version, wheel_sha256=expected)
    metadata_url, wheel_url, metadata = fixtures["pypi"]
    altered = Index({metadata_url: encode(metadata), wheel_url: wheel + b"altered"})
    rejects(production, altered, "Published file bytes differ")
    if altered.requests != [metadata_url, wheel_url]:
        raise RuntimeError("Byte mismatch probe did not reach the expected PyPI wheel URL")
    metadata["urls"][0]["url"] = f"https://test-files.pythonhosted.org/packages/{filename}"
    wrong_host = Index({metadata_url: encode(metadata)})
    rejects(production, wrong_host, "Unexpected wheel origin")
    if wrong_host.requests != [metadata_url]:
        raise RuntimeError("Production verification attempted to fetch from a different index")
    preview = argparse.Namespace(index="pypi", version="0.4.0rc3", wheel_sha256=expected)
    rejects(preview, Index({}), "Production requires a final version")
    results["negative"] = ["altered_bytes", "wrong_wheel_host", "prerelease_to_production"]
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
