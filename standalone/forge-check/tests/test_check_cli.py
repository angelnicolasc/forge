"""Exercise the installed wheel's console command against the public support fixtures."""

from __future__ import annotations

import json
import os
import subprocess
import sysconfig
import tempfile
import unittest
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[3] / "examples" / "check-support"
FORGE = Path(sysconfig.get_path("scripts")) / ("forge.exe" if os.name == "nt" else "forge")


class InstalledCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.reports = Path(os.environ.get("FORGE_CHECK_REPORT_DIR", self.work))
        self.reports.mkdir(parents=True, exist_ok=True)

    def run_check(self, candidate, verdict, code, *, baseline=True):
        output = self.reports / f"{self.id().split('.')[-1]}.json"
        output.unlink(missing_ok=True)
        command = [FORGE, "check", "--cases", FIXTURES / "cases.json", "--candidate", candidate]
        if baseline:
            command.extend(["--baseline", FIXTURES / "baseline.json"])
        command.extend(["--output", output])
        process = subprocess.run(command, cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, code, process.stdout + process.stderr)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["verdict"], verdict)
        self.assertEqual(report["exit_code"], code)
        stream = process.stderr if code == 3 else process.stdout
        self.assertIn(f"{verdict} (exit {code})", stream)
        return report, stream

    def test_pass(self):
        report, _ = self.run_check(FIXTURES / "candidate-pass.json", "pass", 0)
        self.assertEqual(report["baseline_revision"], "r41")
        self.assertEqual(report["candidate_revision"], "r43")
        self.assertEqual(
            report["coverage"],
            {"eligible": 24, "baseline_observed": 24, "candidate_observed": 24, "comparable": 24},
        )
        expected_ids = [f"S{number:02d}" for number in range(1, 25)]
        self.assertCountEqual([case["id"] for case in report["cases"]], expected_ids)
        self.assertEqual({case["result"] for case in report["cases"]}, {"pass"})

    def test_confirmed_misses(self):
        report, console = self.run_check(FIXTURES / "candidate-fail.json", "fail", 1)
        failures = [case for case in report["cases"] if case["result"] == "fail"]
        self.assertCountEqual([case["id"] for case in failures], ["S07", "S08"])
        for case in failures:
            self.assertEqual(case["expected"], "human")
            self.assertEqual(case["candidate"], {"status": "completed", "observed": "self_service"})
            self.assertIn(f"{case['id']} [fail]", console)
        self.assertEqual(report["coverage"]["comparable"], 24)

    def test_missing_baseline(self):
        report, console = self.run_check(
            FIXTURES / "candidate-pass.json", "insufficient", 2, baseline=False
        )
        self.assertIsNone(report["baseline_revision"])
        self.assertEqual(
            report["coverage"],
            {"eligible": 24, "baseline_observed": 0, "candidate_observed": 24, "comparable": 0},
        )
        self.assertIn("Baseline missing", console)

    def test_confirmed_miss_without_baseline(self):
        report, _ = self.run_check(FIXTURES / "candidate-fail.json", "fail", 1, baseline=False)
        self.assertIsNone(report["baseline_revision"])
        failures = [case["id"] for case in report["cases"] if case["result"] == "fail"]
        self.assertCountEqual(failures, ["S07", "S08"])

    def test_missing_case_stays_in_denominator(self):
        candidate = json.loads((FIXTURES / "candidate-pass.json").read_text(encoding="utf-8"))
        candidate["cases"] = [case for case in candidate["cases"] if case["id"] != "S07"]
        partial = self.work / "partial.json"
        partial.write_text(json.dumps(candidate), encoding="utf-8")
        report, _ = self.run_check(partial, "insufficient", 2)
        self.assertEqual(
            report["coverage"],
            {"eligible": 24, "baseline_observed": 24, "candidate_observed": 23, "comparable": 23},
        )
        missing = [case for case in report["cases"] if case["result"] == "insufficient"]
        self.assertEqual([case["id"] for case in missing], ["S07"])
        self.assertIn("candidate_case_missing", missing[0]["reasons"])

    def test_invalid_input_writes_diagnostic(self):
        broken = self.work / "invalid.json"
        broken.write_text("{", encoding="utf-8")
        report, diagnostic = self.run_check(broken, "invalid", 3)
        self.assertIn("Invalid JSON", report["diagnostic"])
        self.assertIn(report["diagnostic"], diagnostic)

    def test_rejects_same_revision(self):
        report, diagnostic = self.run_check(FIXTURES / "baseline.json", "invalid", 3)
        self.assertIn("distinct revision IDs", report["diagnostic"])
        self.assertIn(report["diagnostic"], diagnostic)


if __name__ == "__main__":
    unittest.main()
