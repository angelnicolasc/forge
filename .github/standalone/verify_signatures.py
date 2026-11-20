"""Verify the candidate's raw Git commit series against protected SSH trust inputs."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

_COMMIT = re.compile(r"[0-9a-f]{40}")
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_PRINCIPAL = re.compile(r"[A-Za-z0-9._+@-]+")
_ADDITIONAL_ATTRIBUTION = re.compile(
    r"co[-_\s]*(?:author|develop|commit)|(?:authored|written|developed|generated|signed[-_ ]off)"
    r"[-_ ]by\s*:",
)


def verify(
    repo: Path, base: str, head: str, allowed: Path, principal: str, fingerprint: str
) -> list[str]:
    if not _COMMIT.fullmatch(base) or not _COMMIT.fullmatch(head) or base == head:
        raise ValueError("Require distinct full protected base and candidate commit IDs")
    if not _FINGERPRINT.fullmatch(fingerprint) or not _PRINCIPAL.fullmatch(principal):
        raise ValueError("Require protected principal and SSH fingerprint inputs")
    line = allowed.read_text(encoding="utf-8").splitlines()
    if len(line) != 1 or not line[0].startswith(f"{principal} ssh-ed25519 "):
        raise ValueError("Require one protected Ed25519 allowed-signers record")
    options = [
        "git",
        "-C",
        str(repo),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "gpg.format=ssh",
        "-c",
        "gpg.ssh.program=ssh-keygen",
        "-c",
        f"gpg.ssh.allowedSignersFile={allowed.resolve()}",
        "-c",
        "gpg.minTrustLevel=fully",
    ]
    env = {
        # Do not allow replacements or machine-wide Git trust configuration.
        **os.environ,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }

    def git(*arguments: str) -> str:
        result = subprocess.run(
            [*options, *arguments],
            env=env,
            capture_output=True,
            check=True,
            timeout=60,
        )
        return result.stdout.decode("utf-8")

    if git("rev-parse", "--is-shallow-repository").strip() != "false":
        raise ValueError("Full Git history is required")
    if git("rev-parse", f"{base}^{{commit}}", f"{head}^{{commit}}").splitlines() != [base, head]:
        raise ValueError("Commit inputs do not identify their exact Git objects")
    git("merge-base", "--is-ancestor", base, head)
    commits = git("rev-list", "--reverse", "--topo-order", f"{base}..{head}").splitlines()
    if not commits or commits[-1] != head:
        raise ValueError("No complete candidate commit series")
    expected = f"angelnicolasc|{principal}|angelnicolasc|{principal}|G|{fingerprint}|{principal}"
    identity_format = "%an|%ae|%cn|%ce|%G?|%GF|%GS"
    for commit in commits:
        raw = git("cat-file", "commit", commit)
        headers, separator, message = raw.partition("\n\n")
        if not separator or not re.search(r"^gpgsig -----BEGIN SSH SIGNATURE-----$", headers, re.M):
            raise ValueError(f"Commit {commit} is missing an SSH signature")
        git("verify-commit", commit)
        if (
            git("show", "-s", "--no-use-mailmap", f"--format={identity_format}", commit).strip()
            != expected
        ):
            raise ValueError(f"Commit {commit} has an unauthorized author, committer or signer")
        normalized = unicodedata.normalize("NFKC", message).casefold()
        normalized = "".join(
            character for character in normalized if unicodedata.category(character) != "Cf"
        )
        if _ADDITIONAL_ATTRIBUTION.search(normalized):
            raise ValueError(f"Commit {commit} contains an additional authorship attribution")
    return commits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--allowed-signers", type=Path, required=True)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--fingerprint", required=True)
    arguments = parser.parse_args()
    try:
        commits = verify(
            arguments.repo,
            arguments.base,
            arguments.head,
            arguments.allowed_signers,
            arguments.principal,
            arguments.fingerprint,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        reason = (
            "Git could not verify the complete signed series"
            if isinstance(error, subprocess.SubprocessError)
            else str(error)
        )
        print(reason, file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "base": arguments.base,
                "head": arguments.head,
                "verified_commits": commits,
                "signer_fingerprint": arguments.fingerprint,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
