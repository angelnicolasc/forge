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
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib import error, parse, request

_REPOSITORY = "angelnicolasc/forge"
_PROJECT = "forge-os-check"
_VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)rc[1-9][0-9]*"
_WHEEL = re.compile(rf"forge_os_check-({_VERSION})-py3-none-any\.whl")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_LAB = re.compile(r"[1-9][0-9]*")
_EVIDENCE = ("p23.json", "onboarding.json", "bindings.json")
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
        raise ValueError("The standalone channel accepts one universal release candidate wheel")
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
    return version


def acceptance(
    reports: dict[str, bytes], *, version: str, sha256: str, baseline: str, lab_id: str
) -> None:
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
    reports = {
        name: read_file(path, _MAX_MEMBER)
        for name, path in zip(_EVIDENCE, (args.p23, args.onboarding, args.bindings), strict=True)
    }
    acceptance(
        reports,
        version=version,
        sha256=args.wheel_sha256,
        baseline=args.baseline_sha256,
        lab_id=lab_id,
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
        fixed = {"receipt.json", *_EVIDENCE}
        names = {entry.filename for entry in entries}
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
        name = (names - fixed).pop()
        metadata = report(archive.read("receipt.json"))
        wheel = archive.read(name)
        reports = {filename: archive.read(filename) for filename in _EVIDENCE}
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
    url = f"https://test.pypi.org/pypi/{_PROJECT}/{args.version}/json"
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    for retry in range(7):
        try:
            with opener.open(url, timeout=20) as response:
                metadata = report(read_limited(response, _MAX_MEMBER))
            file = [entry for entry in metadata["urls"] if entry["filename"] == filename]
            if file:
                break
        except error.HTTPError as exception:
            if exception.code != 404:
                raise
        if retry == 6:
            raise ValueError("Published wheel never appeared in TestPyPI")
        time.sleep(10)
    require(len(file) == 1, "Expected exactly one published wheel")
    item = file[0]
    require(
        item["digests"]["sha256"] == args.wheel_sha256, "TestPyPI reports a different wheel digest"
    )
    endpoint = parse.urlsplit(item["url"])
    require(
        endpoint.scheme == "https"
        and endpoint.netloc in {"test-files.pythonhosted.org", "files.pythonhosted.org"}
        and endpoint.path.endswith(f"/{filename}")
        and not endpoint.query
        and not endpoint.fragment,
        "Unexpected TestPyPI wheel origin",
    )
    with opener.open(item["url"], timeout=30) as response:
        wheel = read_limited(response, _MAX_MEMBER)
    require(hmac.compare_digest(digest(wheel), args.wheel_sha256), "Published file bytes differ")
    return {"version": args.version, "wheel_sha256": args.wheel_sha256, "bytes": len(wheel)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    packing = actions.add_parser("seal")
    checking = actions.add_parser("verify")
    for command in (packing, checking):
        command.add_argument("--commit", required=True)
        command.add_argument("--wheel-sha256", required=True)
        command.add_argument("--baseline-sha256", required=True)
    packing.add_argument("--wheel", type=Path, required=True)
    for name in ("p23", "onboarding", "bindings", "output"):
        packing.add_argument(f"--{name}", type=Path, required=True)
    checking.add_argument("--bundle", type=Path, required=True)
    checking.add_argument("--bundle-sha256", required=True)
    checking.add_argument("--vast-id", required=True)
    checking.add_argument("--stage", type=Path)
    transfer = actions.add_parser("fetch")
    transfer.add_argument("--host", required=True)
    transfer.add_argument("--bundle-sha256", required=True)
    transfer.add_argument("--output", type=Path, required=True)
    index = actions.add_parser("verify-index")
    index.add_argument("--version", required=True)
    index.add_argument("--wheel-sha256", required=True)
    args = parser.parse_args()
    try:
        operation = {"seal": seal, "verify": verify, "fetch": fetch, "verify-index": check_index}
        print(json.dumps(operation[args.action](args), sort_keys=True))
    except (error.URLError, TimeoutError):
        print("Preview transfer failed; verify the authorized origin and artifact", file=sys.stderr)
        return 1
    except (OSError, zipfile.BadZipFile, ValueError, KeyError, TypeError) as exception:
        print(f"Preview artifact rejected: {exception}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
