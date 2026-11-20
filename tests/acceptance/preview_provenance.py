"""Exercise the preview publishing gate against a sealed Vast candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(document: dict) -> bytes:
    return (json.dumps(document, sort_keys=True) + "\n").encode()


def altered_bundle(original: dict, case: str, output: Path) -> str:
    members = dict(original)
    manifest = json.loads(members["receipt.json"])
    if case == "missing_conditional":
        del members["conditional.json"]
        del manifest["evidence_sha256"]["conditional.json"]
    else:
        if case in {"build_commit", "package_tree"}:
            name = "build.json"
        elif case == "rc2_pending_canary":
            name = "gate-upgrade.json"
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
        else:
            raise ValueError(f"Unknown release probe {case}")
        members[name] = encode(proof)
        manifest["evidence_sha256"][name] = digest(members[name])
    members["receipt.json"] = encode(manifest)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return digest(output.read_bytes())


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
    with tempfile.TemporaryDirectory(prefix="forge-provenance-") as temporary:
        root = Path(temporary)

        def verify(bundle: Path, sha256: str, stage: Path) -> subprocess.CompletedProcess:
            return subprocess.run(
                [
                    *command,
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
        valid = verify(args.bundle, args.bundle_sha256, stage)
        if valid.returncode != 0:
            raise RuntimeError(valid.stderr)
        (wheel,) = stage.iterdir()
        if digest(wheel.read_bytes()) != args.wheel_sha256:
            raise RuntimeError("Positive staging produced different wheel bytes")
        with zipfile.ZipFile(args.bundle) as archive:
            original = {name: archive.read(name) for name in archive.namelist()}
        outcomes = {}
        for case, expected in expected_errors.items():
            altered = root / f"{case}.zip"
            altered_sha = altered_bundle(original, case, altered)
            stage = root / case
            result = verify(altered, altered_sha, stage)
            if result.returncode != 1 or expected not in result.stderr or stage.exists():
                raise RuntimeError(f"Unexpected decision for {case}: {result.stderr}")
            outcomes[case] = {"exit": result.returncode, "reason": expected, "staged": False}
    receipt = {
        "status": "pass",
        "commit": args.commit,
        "wheel_sha256": args.wheel_sha256,
        "bundle_sha256": args.bundle_sha256,
        "vast_lab_id": args.vast_id,
        "positive_stage_sha256": args.wheel_sha256,
        "negative_probes": outcomes,
    }
    with args.output.open("xb") as output:
        output.write(encode(receipt))


if __name__ == "__main__":
    main()
