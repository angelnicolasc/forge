"""Seal Vast acceptance evidence and stage exactly one approved standalone wheel."""

from __future__ import annotations

import argparse
import email
import hashlib
import hmac
import io
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib import error, parse, request

_REPOSITORY = "angelnicolasc/forge"
_PROJECT = "forge-os-check"
_VERSION_NUMBER = r"(?:0|[1-9][0-9]*)"
_VERSION = re.compile(
    rf"{_VERSION_NUMBER}\.{_VERSION_NUMBER}\.{_VERSION_NUMBER}(?P<rc>rc[1-9][0-9]*)?"
)
_WHEEL = re.compile(rf"forge_os_check-({_VERSION.pattern})-py3-none-any\.whl")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_LAB = re.compile(r"[1-9][0-9]*")
_EVIDENCE = ("p23.json", "onboarding.json", "bindings.json")
_ADR_EVIDENCE = ("build.json", "conditional.json", "gate-upgrade.json")
_RC2_WHEEL_SHA256 = "da2e99069084178ca8b85ebcf9504e0ab7be8755bc92b8f355d0eddd043713ec"
_RC3_WHEEL_SHA256 = "866abed30badeddecbd655aa402264269b81c879ff6d6cc75acafc2bd63e2ae0"
_LEGACY_RELEASE_CANDIDATES = {
    "0.4.0rc1": "ac7fc305fbe5bd25b92d2edc01e64e7ceee9d9b269f1700005f3c0711993b632",
    "0.4.0rc2": _RC2_WHEEL_SHA256,
}
_STABLE_EVIDENCE = ("stable-compatibility.json",)
_INDEXES = {
    "testpypi": ("test.pypi.org", {"test-files.pythonhosted.org", "files.pythonhosted.org"}),
    "pypi": ("pypi.org", {"files.pythonhosted.org"}),
}
_MAX_BUNDLE = 8 * 1024 * 1024
_MAX_MEMBER = 2 * 1024 * 1024


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(value: Any, message: str) -> None:
    if not value:
        raise ValueError(message)


def read_limited(stream: Any, limit: int) -> bytes:
    data = stream.read(limit + 1)
    require(len(data) <= limit, "Preview artifact exceeds its size limit")
    return data


def read_file(path: Path, limit: int) -> bytes:
    require(path.is_file() and not path.is_symlink(), "Expected one regular preview artifact")
    with path.open("rb") as stream:
        return read_limited(stream, limit)


def report(data: bytes) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in values:
            require(key not in document, "Duplicate JSON object field in preview evidence")
            document[key] = value
        return document

    value = json.loads(data, object_pairs_hook=pairs)
    require(isinstance(value, dict), "Expected a JSON object in preview evidence")
    return value


def wheel_version(name: str, data: bytes) -> str:
    match = _WHEEL.fullmatch(name)
    if not match:
        raise ValueError("Expected one universal standalone final or release candidate wheel")
    version = match[1]
    expected_metadata = f"forge_os_check-{version}.dist-info/METADATA"
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        records = [entry for entry in wheel.infolist() if entry.filename == expected_metadata]
        require(len(records) == 1, "Missing or ambiguous standalone wheel metadata")
        require(records[0].file_size <= 32_768, "Standalone wheel metadata exceeds its size limit")
        with wheel.open(records[0]) as stream:
            metadata = email.message_from_bytes(read_limited(stream, 32_768))
        require(metadata.get_all("Name") == [_PROJECT], "Unexpected wheel metadata project")
        require(metadata.get_all("Version") == [version], "Wheel metadata version mismatch")
        if is_stable(version):
            require(
                metadata.get_all("Requires-Python") == [">=3.11"],
                "Stable Python support must match the approved 3.11 minimum",
            )
    return version


def is_stable(version: str) -> bool:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError("Expected a canonical standalone release version")
    return match["rc"] is None


def evidence_names(version: str) -> tuple[str, ...]:
    final = is_stable(version)
    # Only these two already-sealed candidates predate conditional evidence.
    if version in _LEGACY_RELEASE_CANDIDATES:
        return _EVIDENCE
    names = _EVIDENCE + _ADR_EVIDENCE
    return names + _STABLE_EVIDENCE if final else names


def git(repo: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
    ).stdout


def source_trees(repo: Path | None, commit: str) -> dict[str, str]:
    if repo is None:
        raise ValueError("The approved source Git repository is required")
    return {
        "source_tree": git(repo, "rev-parse", "--verify", f"{commit}^{{tree}}").decode().strip(),
        "package_tree": git(repo, "rev-parse", "--verify", f"{commit}:standalone/forge-check")
        .decode()
        .strip(),
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    lab_id = os.environ.get("FORGE_VAST_LAB_ID", "")
    require(sys.platform == "linux" and _LAB.fullmatch(lab_id), "Build on the designated Vast VM")
    require(_COMMIT.fullmatch(args.commit), "Require the full candidate commit SHA")
    require(not args.output.exists(), "Build receipt already exists")
    dist = args.dist.resolve()
    dist.mkdir(parents=True, exist_ok=True)
    require(not any(dist.iterdir()), "Use an empty wheel output directory")
    trees = source_trees(args.repo, args.commit)
    with tempfile.TemporaryDirectory(prefix="forge-signed-source-") as folder:
        package = Path(folder) / "standalone" / "forge-check"
        archive = git(args.repo, "archive", "--format=tar", args.commit, "standalone/forge-check")
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            require(all(m.isfile() or m.isdir() for m in tar), "Expected a regular package tree")
            tar.extractall(folder, filter="data")
        subprocess.run(
            [
                args.uv,
                "build",
                str(package),
                "--no-sources",
                "--wheel",
                "--out-dir",
                str(dist),
                "--python",
                sys.executable,
            ],
            check=True,
            cwd=folder,
        )
    # uv creates a .gitignore beside the distribution in a fresh output directory.
    wheels = [entry for entry in dist.iterdir() if entry.name != ".gitignore"]
    require(len(wheels) == 1, "Build must produce exactly one wheel")
    wheel = wheels[0]
    data = read_file(wheel, _MAX_MEMBER)
    version = wheel_version(wheel.name, data)
    proof = {
        "schema": 1,
        "commit": args.commit,
        **trees,
        "version": version,
        "wheel": wheel.name,
        "wheel_sha256": digest(data),
        "vast_lab_id": lab_id,
    }
    with args.output.open("x", encoding="utf-8") as destination:
        destination.write(json.dumps(proof, sort_keys=True) + "\n")
    return proof


def conditional_evidence(proof: dict[str, Any]) -> None:
    expected = {
        "equal": "pass",
        "above": "pass",
        "complete": "pass",
        "cached": "pass",
        "omitted": "fail",
        "partial": "insufficient",
        "unknown-primary": "insufficient",
        "unconfirmed-reserve": "insufficient",
        "wrong-identity": "insufficient",
        "expired-cache": "insufficient",
    }
    required = {(route, name) for route in ("direct", "code_mode") for name in expected}
    seen = set()
    for row in proof["matrix"]:
        key = (row["route"], row["scenario"])
        require(key not in seen, "Duplicate conditional evidence case")
        seen.add(key)
        if key not in required:
            continue
        verdict = expected[row["scenario"]]
        before, after = row["alias_before"], row["alias_after"]
        decision = row["decision"]
        require(
            row["gate"]["report"]["verdict"] == verdict
            and row["evaluation_dispatches"] == 0
            and row["executor"]["status"] == "completed",
            "Incorrect conditional verdict, executor state, or verifier dispatch",
        )
        if verdict == "pass":
            require(
                decision["status"] == "applied"
                and after["generation"] == before["generation"] + 1
                and after["revision"] == row["gate"]["candidate_revision"],
                "Passing conditional promotion did not activate its candidate",
            )
        else:
            require(
                decision["status"] == "rejected" and before == after,
                "Conditional promotion must preserve the alias",
            )
    require(required <= seen, "Missing required conditional cases or route")
    consistency = proof["consistency"]
    for name, verdict in {
        "consistent": "pass",
        "repeat_and_permute": "pass",
        "one_receipt_two_subjects": "insufficient",
        "one_receipt_two_cases": "insufficient",
        "different_reserve_quantities": "insufficient",
        "cache_valid": "pass",
        "cache_at_expiry": "insufficient",
        "legacy_cache_without_expiry": "insufficient",
        "two_hits_one_consistent_origin": "pass",
        "conflicting_origin_for_two_hits": "insufficient",
        "cache_receipt_as_own_origin": "insufficient",
    }.items():
        require(consistency.get(name) == verdict, "Missing or incorrect receipt consistency result")
    legacy = proof["legacy"]
    require(
        legacy["decision"]["status"] == "applied"
        and legacy["saved_check"]["verdict"] == "pass"
        and legacy["gate"]["report"] == legacy["saved_check"],
        "Legacy scope must retain the saved report and promotion behavior",
    )
    expired = proof["promotion_expiry"]
    require(
        expired["decision"]["reason"] == "evidence_expired"
        and expired["decision"]["status"] == "rejected"
        and expired["alias_before"] == expired["alias_after"]
        and expired["evaluation_dispatches"] == 0,
        "Expired evidence must leave the alias unchanged",
    )


def gate_upgrade(proof: dict[str, Any], version: str, sha256: str, lab_id: str) -> None:
    require(
        proof["status"] == "pass"
        and proof["vast_lab_id"] == lab_id
        and proof["rc2_sha256"] == _RC2_WHEEL_SHA256
        and proof["wheel_sha256"] == sha256
        and proof["candidate"] == version,
        "Gate upgrade evidence must identify the RC2 wheel and this candidate and host",
    )
    results = proof["outcomes"]
    for route, provider_calls in (("canary", 0), ("shadow", 1)):
        observed = results[route]
        require(
            observed["prior_certificate"] == "forge-check/2"
            and observed["new_certificate"] == "forge-check/2.1"
            and observed["historical_verdict"] == observed["corrected_verdict"] == "pass"
            and observed["reevaluated_verdict"] == "insufficient"
            and observed["rollout_hold"] == "evidence_revalidation_required"
            and observed["existing_root_provider_calls"] == provider_calls
            and observed["new_generation"] == 2,
            f"Gate upgrade failed the {route} compatibility policy",
        )
    require(
        results["applied"]["historical_decision"] == "replayed"
        and results["applied"]["rollback"] == "applied"
        and results["legacy"]["rc2_verifier"] == "forge-check/1"
        and results["legacy"]["legacy_promotion"] == "applied",
        "Gate upgrade must preserve prior decisions, rollback and legacy promotions",
    )


def stable_compatibility(
    proof: dict[str, Any], *, version: str, sha256: str, lab_id: str, commit: str
) -> None:
    require(
        type(proof.get("schema")) is int
        and proof["schema"] == 1
        and proof["status"] == "pass"
        and proof["commit"] == commit
        and proof["version"] == version
        and proof["wheel_sha256"] == sha256
        and proof["vast_lab_id"] == lab_id
        and proof["previous_version"] == "0.4.0rc3"
        and proof["previous_wheel_sha256"] == _RC3_WHEEL_SHA256
        and proof["platform"] == "linux-x86_64",
        "Stable compatibility must identify this commit, wheel, host and the approved RC3 wheel",
    )
    installs = proof["installs"]
    require(set(installs) == {"3.11", "3.12"}, "Require Python 3.11 and 3.12 installs")
    for minor, install in installs.items():
        require(
            re.fullmatch(rf"{re.escape(minor)}\.[0-9]+", install["python"])
            and install["base_dependency_free"] is True
            and install["base_verdicts"]
            == {"pass": "pass", "fail": "fail", "insufficient": "insufficient"}
            and install["extra_inventory"]
            == {
                "direct": {"on_hand": 7, "sku": "A-18"},
                "dispatches": ["A-18", "A-18", "B-42"],
                "program": {"total_on_hand": 9},
            },
            f"Stable compatibility failed the Python {minor} base or Code Mode install",
        )
    upgrade = proof["upgrade"]
    observations = upgrade["destination"]
    require(
        upgrade["schema_before"] == upgrade["schema_after"] == 2
        and _SHA256.fullmatch(upgrade["backup_sha256"])
        and _SHA256.fullmatch(upgrade["historical_receipts_sha256"])
        and upgrade["historical_receipts_sha256"] == upgrade["post_install_receipts_sha256"]
        and type(upgrade["receipt_count_before"]) is int
        and type(upgrade["receipt_count_after"]) is int
        and 0 < upgrade["receipt_count_before"] < upgrade["receipt_count_after"]
        and upgrade["alias_before"] == upgrade["alias_after"] == upgrade["alias_after_rollback"]
        and _SHA256.fullmatch(upgrade["alias_before"]["revision"])
        and type(upgrade["alias_before"]["generation"]) is int
        and upgrade["alias_before"]["generation"] > 0
        and upgrade["rollback_live_receipts_preserved"] is True
        and [row["attempt"] for row in observations]
        == ["before-stable", "after-stable", "after-rollback"]
        and all(
            row["revision"] == upgrade["alias_before"]["revision"]
            and row["binding"] == "inventory.stock"
            and row["input"] == {"sku": "RELEASE-SKU"}
            for row in observations
        ),
        "Stable compatibility must preserve the live RC3 ledger and demonstrate rollback",
    )


def acceptance(
    reports: dict[str, bytes],
    *,
    version: str,
    sha256: str,
    baseline: str,
    lab_id: str,
    commit: str,
    wheel: str,
    repo: Path | None,
) -> None:
    require(
        set(reports) == set(evidence_names(version)), "Incomplete standalone acceptance evidence"
    )
    if version in _LEGACY_RELEASE_CANDIDATES:
        require(
            sha256 == _LEGACY_RELEASE_CANDIDATES[version],
            "Legacy evidence is valid only for the originally sealed wheel",
        )
    p23 = report(reports["p23.json"])
    proof = p23.get("wheel_sha256")
    require(
        p23.get("status") == "pass"
        and p23.get("vast_lab_id") == lab_id
        and p23.get("platform") == "x86_64"
        and str(p23.get("python")).startswith("3.12.")
        and isinstance(proof, dict)
        and proof.get(version) == sha256
        and proof.get("0.3.0") == baseline
        and p23.get("published") is False,
        "P23 must cover these exact candidate and baseline wheels on the approved Vast host",
    )
    rollback = p23["rollback"]
    require(
        rollback["live_schema"] == rollback["pre_upgrade_schema"] == 2
        and rollback["old_reader_inspected_compatible_ledger"] is True
        and rollback["old_client_refused_v2_dispatch"] is True
        and rollback["resumed_writes_from_old_snapshot"] is False
        and rollback["receipts_after"] > rollback["receipts_before"] > 0,
        "P23 rollback or state retention evidence did not meet the preview policy",
    )
    index = p23["temporary_index"]
    valid, altered = index["rows"]
    require(
        index["status"] == "pass"
        and valid["variant"] == "valid"
        and valid["delivered_sha256"] == sha256
        and valid["installer_exit"] == 0
        and valid["installed_cli_usable"] is True
        and altered["variant"] == "altered"
        and altered["delivered_sha256"] != sha256
        and altered["installer_exit"] != 0
        and altered["installed_cli_usable"] is False
        and altered["reason"] == "hash mismatch"
        and valid["candidate_downloaded"] is True
        and altered["candidate_downloaded"] is True,
        "P23 must install the approved bytes and reject a downloaded altered wheel",
    )
    onboarding = report(reports["onboarding.json"])
    totals = onboarding.get("totals", {})
    require(
        onboarding.get("status") == "pass"
        and onboarding.get("instance_id") == lab_id
        and onboarding.get("wheel_sha256") == sha256
        and totals.get("assigned_repos") == 5
        and totals.get("onboarding_correct_under_600s", 0) >= 4
        and totals.get("executed_changes") == totals.get("correct_changes") == 15
        and totals.get("false_greens") == 0
        and totals.get("repos_three_changes_stable") == 5,
        "P22-Lab must establish all required synthetic CI outcomes for the approved wheel",
    )
    bindings = report(reports["bindings.json"])
    require(
        bindings.get("status") == "pass"
        and bindings.get("lab_id") == lab_id
        and bindings.get("version") == version
        and bindings.get("wheel_sha256") == sha256
        and all(
            bindings["cases"].get(case) is True
            for case in ("authorization_zero_dispatch", "cache_isolation", "prior_reservation")
        ),
        "Installed binding and Code Mode evidence must identify this wheel and Vast host",
    )
    if "build.json" in reports:
        require(
            report(reports["build.json"])
            == {
                "schema": 1,
                "commit": commit,
                **source_trees(repo, commit),
                "version": version,
                "wheel": wheel,
                "wheel_sha256": sha256,
                "vast_lab_id": lab_id,
            },
            "Build receipt must identify the approved commit, Git trees, wheel and Vast host",
        )
        conditional = report(reports["conditional.json"])
        require(
            conditional["status"] == "pass"
            and conditional["version"] == version
            and conditional["wheel_sha256"] == sha256
            and conditional["vast_lab_id"] == lab_id,
            "Conditional evidence is missing or describes a different candidate",
        )
        conditional_evidence(conditional)
        gate_upgrade(report(reports["gate-upgrade.json"]), version, sha256, lab_id)
    if is_stable(version):
        stable_compatibility(
            report(reports["stable-compatibility.json"]),
            version=version,
            sha256=sha256,
            lab_id=lab_id,
            commit=commit,
        )


def check_inputs(commit: str, wheel_sha: str, baseline: str, lab_id: str) -> None:
    require(_COMMIT.fullmatch(commit), "Require a full candidate commit SHA")
    require(_SHA256.fullmatch(wheel_sha), "Require an independently expected wheel SHA-256")
    require(_SHA256.fullmatch(baseline), "Require an independently approved baseline SHA-256")
    require(_LAB.fullmatch(lab_id), "Require an approved Vast instance ID")


def seal(args: argparse.Namespace) -> dict[str, Any]:
    lab_id = os.environ.get("FORGE_VAST_LAB_ID", "")
    require(
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and sys.version_info[:2] == (3, 12),
        "Seal the evidence on the designated Vast Linux x86_64 Python 3.12 host",
    )
    check_inputs(args.commit, args.wheel_sha256, args.baseline_sha256, lab_id)
    wheel = read_file(args.wheel, _MAX_MEMBER)
    name = args.wheel.name
    version = wheel_version(name, wheel)
    require(hmac.compare_digest(digest(wheel), args.wheel_sha256), "Wheel SHA-256 mismatch")
    paths = dict(zip(_EVIDENCE, (args.p23, args.onboarding, args.bindings), strict=True))
    if "build.json" in evidence_names(version):
        require(
            args.build_report and args.conditional and args.gate_upgrade,
            "Require build, conditional and Gate upgrade receipts",
        )
        paths.update(
            {
                "build.json": args.build_report,
                "conditional.json": args.conditional,
                "gate-upgrade.json": args.gate_upgrade,
            }
        )
    if is_stable(version):
        require(args.stable_compatibility, "Require RC3 upgrade and stable interpreter receipts")
        paths["stable-compatibility.json"] = args.stable_compatibility
    reports = {filename: read_file(path, _MAX_MEMBER) for filename, path in paths.items()}
    acceptance(
        reports,
        version=version,
        sha256=args.wheel_sha256,
        baseline=args.baseline_sha256,
        lab_id=lab_id,
        commit=args.commit,
        wheel=name,
        repo=args.repo,
    )
    metadata = {
        "schema": 1,
        "repository": _REPOSITORY,
        "commit": args.commit,
        "version": version,
        "wheel": name,
        "wheel_sha256": args.wheel_sha256,
        "baseline_sha256": args.baseline_sha256,
        "vast_lab_id": lab_id,
        "evidence_sha256": {name: digest(data) for name, data in reports.items()},
    }
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("receipt.json", json.dumps(metadata, sort_keys=True) + "\n")
        archive.writestr(name, wheel)
        for name, data in reports.items():
            archive.writestr(name, data)
    bundle = archive_bytes.getvalue()
    require(len(bundle) <= _MAX_BUNDLE, "Sealed preview bundle exceeds its size limit")
    with args.output.open("xb") as destination:
        destination.write(bundle)
    return {**metadata, "bundle_sha256": digest(bundle)}


def verify(args: argparse.Namespace) -> dict[str, Any]:
    check_inputs(args.commit, args.wheel_sha256, args.baseline_sha256, args.vast_id)
    require(_SHA256.fullmatch(args.bundle_sha256), "Require an externally approved bundle SHA-256")
    bundle = read_file(args.bundle, _MAX_BUNDLE)
    require(
        hmac.compare_digest(digest(bundle), args.bundle_sha256), "Approved bundle SHA-256 mismatch"
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        entries = archive.infolist()
        names = {entry.filename for entry in entries}
        wheels = [entry.filename for entry in entries if _WHEEL.fullmatch(entry.filename)]
        require(len(wheels) == 1, "Expected one wheel in the preview bundle")
        name = wheels[0]
        match = _WHEEL.fullmatch(name)
        assert match is not None
        version = match[1]
        final = is_stable(version)
        require(not args.require_stable or final, "This release channel requires a final version")
        require(
            not args.require_preview or not final,
            "This release channel requires a release candidate",
        )
        evidence = evidence_names(version)
        fixed = {"receipt.json", *evidence}
        require(
            len(entries) == len(names) == len(fixed) + 1
            and fixed <= names
            and all(
                entry.file_size <= _MAX_MEMBER
                and not entry.flag_bits & 1
                and stat.S_IFMT(entry.external_attr >> 16) in {0, stat.S_IFREG}
                and (entry.filename in fixed or _WHEEL.fullmatch(entry.filename))
                for entry in entries
            ),
            "Preview bundle contains duplicate, unsafe, or unexpected entries",
        )
        metadata = report(archive.read("receipt.json"))
        wheel = archive.read(name)
        reports = {filename: archive.read(filename) for filename in evidence}
    require(
        hmac.compare_digest(digest(wheel), args.wheel_sha256), "Approved wheel SHA-256 mismatch"
    )
    version = wheel_version(name, wheel)
    expected = {
        "schema": 1,
        "repository": _REPOSITORY,
        "commit": args.commit,
        "version": version,
        "wheel": name,
        "wheel_sha256": args.wheel_sha256,
        "baseline_sha256": args.baseline_sha256,
        "vast_lab_id": args.vast_id,
        "evidence_sha256": {name: digest(data) for name, data in reports.items()},
    }
    require(metadata == expected, "Bundle receipt differs from approved inputs or evidence")
    acceptance(
        reports,
        version=version,
        sha256=args.wheel_sha256,
        baseline=args.baseline_sha256,
        lab_id=args.vast_id,
        commit=args.commit,
        wheel=name,
        repo=args.repo,
    )
    if args.stage is not None:
        args.stage.mkdir(mode=0o700, exist_ok=False)
        with (args.stage / name).open("xb") as destination:
            destination.write(wheel)
    return {**metadata, "bundle_sha256": args.bundle_sha256}


def fetch(args: argparse.Namespace) -> dict[str, Any]:
    # Keep the signed URL in the protected environment; do not print it or follow redirects.
    url = os.environ.get("STANDALONE_VAST_BUNDLE_URL", "")
    endpoint = parse.urlsplit(url)
    require(
        endpoint.scheme == "https"
        and endpoint.hostname == args.host
        and endpoint.port in {None, 443}
        and endpoint.username is None
        and endpoint.password is None
        and not endpoint.fragment,
        "Protected receipt URL must use the approved HTTPS origin",
    )
    require(_SHA256.fullmatch(args.bundle_sha256), "Require an externally approved bundle SHA-256")
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=30) as response:
        bundle = read_limited(response, _MAX_BUNDLE)
    require(
        hmac.compare_digest(digest(bundle), args.bundle_sha256), "Approved bundle SHA-256 mismatch"
    )
    with args.output.open("xb") as destination:
        destination.write(bundle)
    return {"bundle_sha256": args.bundle_sha256, "bytes": len(bundle)}


def check_index(args: argparse.Namespace) -> dict[str, Any]:
    filename = f"forge_os_check-{args.version}-py3-none-any.whl"
    require(_WHEEL.fullmatch(filename) and _SHA256.fullmatch(args.wheel_sha256), "Invalid approval")
    final = is_stable(args.version)
    require(args.index != "pypi" or final, "Production requires a final version")
    metadata_origin, wheel_origins = _INDEXES[args.index]
    url = f"https://{metadata_origin}/pypi/{_PROJECT}/{args.version}/json"
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    identity = {"index": args.index, "version": args.version}
    for retry in range(7):
        try:
            with opener.open(url, timeout=20) as response:
                metadata = report(read_limited(response, _MAX_MEMBER))
        except error.HTTPError as exception:
            if exception.code != 404:
                raise
            if args.probe:
                return {**identity, "state": "absent"}
        else:
            files = metadata["urls"]
            require(isinstance(files, list), "Index metadata must provide a list of release files")
            matches = [entry for entry in files if entry["filename"] == filename]
            if args.probe or matches:
                break
        if retry == 6:
            raise ValueError(f"Published wheel never appeared on {metadata_origin}")
        time.sleep(10)
    require(
        metadata["info"]["name"] == _PROJECT and metadata["info"]["version"] == args.version,
        "Index metadata does not identify the approved release",
    )
    require(len(matches) == 1, "Expected exactly one published wheel")
    if args.probe or final:
        require(len(files) == 1, "Index contains an unapproved distribution")
    item = matches[0]
    require(item.get("yanked") is False, "Approved wheel is yanked or its status is missing")
    require(
        item["digests"]["sha256"] == args.wheel_sha256, "Index reports a different wheel digest"
    )
    endpoint = parse.urlsplit(item["url"])
    require(
        endpoint.scheme == "https"
        and endpoint.netloc in wheel_origins
        and endpoint.path.endswith(f"/{filename}")
        and not endpoint.query
        and not endpoint.fragment,
        "Unexpected wheel origin for the selected index",
    )
    with opener.open(item["url"], timeout=30) as response:
        wheel = read_limited(response, _MAX_MEMBER)
    require(hmac.compare_digest(digest(wheel), args.wheel_sha256), "Published file bytes differ")
    receipt = {**identity, "wheel_sha256": args.wheel_sha256, "bytes": len(wheel)}
    if args.probe:
        receipt["state"] = "verified"
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    building = actions.add_parser("build")
    building.add_argument("--repo", type=Path, required=True)
    building.add_argument("--commit", required=True)
    building.add_argument("--uv", default="uv")
    building.add_argument("--dist", type=Path, required=True)
    building.add_argument("--output", type=Path, required=True)
    packing = actions.add_parser("seal")
    checking = actions.add_parser("verify")
    for command in (packing, checking):
        command.add_argument("--commit", required=True)
        command.add_argument("--wheel-sha256", required=True)
        command.add_argument("--baseline-sha256", required=True)
        command.add_argument("--repo", type=Path)
    packing.add_argument("--wheel", type=Path, required=True)
    packing.add_argument("--build-report", type=Path)
    packing.add_argument("--conditional", type=Path)
    packing.add_argument("--gate-upgrade", type=Path)
    packing.add_argument("--stable-compatibility", type=Path)
    for name in ("p23", "onboarding", "bindings", "output"):
        packing.add_argument(f"--{name}", type=Path, required=True)
    checking.add_argument("--bundle", type=Path, required=True)
    checking.add_argument("--bundle-sha256", required=True)
    checking.add_argument("--vast-id", required=True)
    checking.add_argument("--stage", type=Path)
    channel = checking.add_mutually_exclusive_group()
    channel.add_argument("--require-stable", action="store_true")
    channel.add_argument("--require-preview", action="store_true")
    transfer = actions.add_parser("fetch")
    transfer.add_argument("--host", required=True)
    transfer.add_argument("--bundle-sha256", required=True)
    transfer.add_argument("--output", type=Path, required=True)
    index = actions.add_parser("verify-index")
    index.add_argument("--index", choices=tuple(_INDEXES), default="testpypi")
    index.add_argument("--probe", action="store_true", help="Check whether the exact wheel exists")
    index.add_argument("--version", required=True)
    index.add_argument("--wheel-sha256", required=True)
    args = parser.parse_args()
    try:
        operation = {
            "build": build,
            "seal": seal,
            "verify": verify,
            "fetch": fetch,
            "verify-index": check_index,
        }
        print(json.dumps(operation[args.action](args), sort_keys=True))
    except (error.URLError, TimeoutError):
        print("Preview transfer failed; verify the authorized origin and artifact", file=sys.stderr)
        return 1
    except (
        OSError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
        ValueError,
        KeyError,
        TypeError,
    ) as exception:
        print(f"Preview artifact rejected: {exception}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
