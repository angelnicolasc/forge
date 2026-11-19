"""Public command for evaluating a pair of recorded agent runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import suppress
from importlib.metadata import version
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Never

from forge_check.contract import (
    INVALID_EXIT,
    SCHEMA_VERSION,
    InputError,
    compare,
    load_cases,
    load_run,
)

if TYPE_CHECKING:
    from forge_check.contract import Report

_TERMINAL_CASE_LIMIT = 10


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # argparse's default diagnostics may include arbitrary argument values.
        self.exit(INVALID_EXIT, "invalid (exit 3) | Run forge check --help for usage.\n")


class _Options(argparse.Namespace):
    cases: Path
    baseline: Path | None
    candidate: Path
    output: Path
    save_reference: Path | None


def _parse_args(argv: list[str] | None) -> _Options:
    parser = _Parser(prog="forge", description="Evaluate saved agent results.", allow_abbrev=False)
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('forge-os-check')}"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser(
        "check", help="Compare runs against a business rule.", allow_abbrev=False
    )
    check.add_argument("--cases", type=Path, required=True, help="Eligible cases and expectations")
    check.add_argument("--baseline", type=Path, help="Recorded reference revision")
    check.add_argument("--candidate", type=Path, required=True, help="Recorded candidate revision")
    check.add_argument("--output", type=Path, required=True, help="Destination for the JSON report")
    check.add_argument(
        "--save-reference", type=Path, help="New pending reference without a baseline"
    )
    options = _Options()
    parser.parse_args(argv, namespace=options)
    return options


def _validate_destination(path: Path, protected: list[Path], role: str) -> None:
    try:
        resolved = path.resolve()
        conflicts = any(resolved == other.resolve() for other in protected)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InputError(f"Cannot resolve {role} or protected path") from exc
    if conflicts:
        raise InputError(f"{role} must use a distinct path")


def _write_json(document: object, path: Path, role: str, *, replace: bool = True) -> None:
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            dir=path.parent, mode="w", encoding="utf-8", newline="\n", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(document, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        if replace:
            os.replace(temporary_path, path)
        else:
            # Linking the complete file publishes it atomically without clobbering a prior run.
            os.link(temporary_path, path)
    except FileExistsError as exc:
        raise InputError(f"{role} already exists. Choose a new path.") from exc
    except (OSError, ValueError) as exc:
        raise InputError(f"Cannot write {role}") from exc
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def _print_report(report: Report, *, reference_saved: bool) -> None:
    print(
        f"{report['verdict']} (exit {report['exit_code']}) "
        f"for {report['rule_id']} from {report['source']}"
    )
    coverage = report["coverage"]
    total = coverage["eligible"]
    print(
        f"Coverage {total} eligible | baseline {coverage['baseline_observed']}/{total} | "
        f"candidate {coverage['candidate_observed']}/{total} | "
        f"comparable {coverage['comparable']}/{total}"
    )
    if report["baseline_revision"] is None:
        print("Baseline missing.")

    actionable = [case for case in report["cases"] if case["result"] != "pass"]
    actionable.sort(key=lambda case: case["result"] != "fail")
    for case in actionable[:_TERMINAL_CASE_LIMIT]:
        baseline = case["baseline"]
        candidate = case["candidate"]
        baseline_observed = baseline["observed"] or "unavailable"
        candidate_observed = candidate["observed"] or "unavailable"
        print(
            f"{case['id']} [{case['result']}] expected {case['expected']}, "
            f"candidate {candidate_observed} ({candidate['status']}), "
            f"baseline {baseline_observed} ({baseline['status']})"
        )
    if len(actionable) > _TERMINAL_CASE_LIMIT:
        print(f"See the JSON report for {len(actionable) - _TERMINAL_CASE_LIMIT} more cases.")
    if reference_saved:
        print("Saved a pending reference for review.")
    print(report["next_action"])


def main(argv: list[str] | None = None) -> int:
    options = _parse_args(argv)
    protected = [options.cases, options.candidate]
    if options.baseline is not None:
        protected.append(options.baseline)
    if options.save_reference is not None:
        protected.append(options.save_reference)

    report_path_valid = False
    reference_saved = False
    try:
        _validate_destination(options.output, protected, "--output")
        report_path_valid = True
        if options.save_reference is not None and options.baseline is not None:
            raise InputError("--save-reference requires a first run without --baseline")
        if options.save_reference is not None:
            _validate_destination(
                options.save_reference, [options.cases, options.candidate], "--save-reference"
            )

        case_set = load_cases(options.cases)
        candidate = load_run(options.candidate, "candidate", case_set)
        baseline = None
        if options.baseline is not None:
            baseline = load_run(options.baseline, "baseline", case_set)
        report = compare(case_set, baseline, candidate)

        if options.save_reference is not None:
            _write_json(
                candidate.pending_reference(case_set),
                options.save_reference,
                "pending reference",
                replace=False,
            )
            reference_saved = True
        _write_json(report, options.output, "JSON report")
        _print_report(report, reference_saved=reference_saved)
        return report["exit_code"]
    except InputError as exc:
        print(f"invalid (exit {INVALID_EXIT}) | {exc}", file=sys.stderr)
        if reference_saved:
            print("Saved the pending reference before the report write failed.", file=sys.stderr)
        if report_path_valid:
            invalid = {
                "schema_version": SCHEMA_VERSION,
                "verdict": "invalid",
                "exit_code": INVALID_EXIT,
                "diagnostic": str(exc),
                "next_action": "Correct the reported problem and rerun forge check.",
            }
            try:
                _write_json(invalid, options.output, "JSON report")
            except InputError as report_error:
                if str(report_error) != str(exc):
                    print(report_error, file=sys.stderr)
        return INVALID_EXIT
