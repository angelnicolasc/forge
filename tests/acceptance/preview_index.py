"""Rehearse publishing the same wheel to a temporary loopback index."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Recorded(SimpleHTTPRequestHandler):
    def do_GET(self):
        self.server.paths.append(self.path)
        super().do_GET()

    def log_message(self, *args):
        pass


def rehearse(wheel, directory, version, digest):
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != digest:
        raise ValueError("El wheel propuesto no coincide con el artefacto ensayado")
    project = directory / "index" / "simple" / "forge-os-check"
    project.mkdir(parents=True)
    candidate = project / wheel.name
    shutil.copyfile(wheel, candidate)
    original = candidate.read_bytes()
    (project / "index.html").write_text(
        f'<a href="{wheel.name}">{wheel.name}</a>\n', encoding="utf-8"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Recorded, directory=directory / "index"))
    server.paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = directory / "requisitos.txt"
        req.write_text(f"forge-os-check=={version} --hash=sha256:{digest}\n", encoding="utf-8")
        results = []
        for profile in ("valid", "altered"):
            venv = directory / profile
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True
            )
            if profile == "altered":
                candidate.write_bytes(original + b"synthetic-digest-negative")
            start = len(server.paths)
            args = [
                str(venv / "bin" / "python"),
                "-Im",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "install",
                "--no-cache-dir",
                "--no-deps",
                "--index-url",
                f"http://127.0.0.1:{server.server_port}/simple/",
                "--require-hashes",
                "-r",
                str(req),
            ]
            env = {"PATH": os.defpath, "HOME": str(venv), "PIP_CONFIG_FILE": os.devnull}
            result = subprocess.run(
                args, env=env, cwd=venv, capture_output=True, text=True, timeout=40
            )
            success = result.returncode == 0
            downloaded = any(path.endswith(wheel.name) for path in server.paths[start:])
            reason = "hash mismatch" if "DO NOT MATCH THE HASHES" in result.stderr.upper() else ""
            command = venv / "bin" / "forge"
            usable = (
                command.exists()
                and subprocess.run(
                    [str(command), "check", "--help"], cwd=venv, env=env, capture_output=True
                ).returncode
                == 0
            )
            results.append(
                {
                    "variant": profile,
                    "installer_exit": result.returncode,
                    "candidate_downloaded": downloaded,
                    "reason": reason,
                    "installed_cli_usable": usable,
                    "delivered_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                }
            )
            if (
                not downloaded
                or (profile == "valid" and not (success and usable))
                or (profile == "altered" and (success or usable or not reason))
            ):
                raise ValueError(
                    f"El ensayo de integridad falló: {results[-1]} {result.stderr[-350:]}"
                )
        return {
            "status": "pass",
            "scope": "PEP503 loopback; external publication pending",
            "rows": results,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    if sys.platform != "linux" or not os.environ.get("FORGE_VAST_LAB_ID"):
        raise SystemExit("Requires Linux and a nonempty FORGE_VAST_LAB_ID.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        print(
            json.dumps(
                rehearse(args.wheel.resolve(), Path(directory), args.version, args.sha256),
                sort_keys=True,
            )
        )
