"""Print the provider-call table or check the table in the project README."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
START = "<!-- bindings-benchmark:start -->"
END = "<!-- bindings-benchmark:end -->"


def render(report: dict) -> str:
    if report["status"] != "pass" or report["benchmark"] != "inventory-bindings":
        raise ValueError("Expected a passing inventory binding benchmark")
    rows = [
        "| Inventory workload | Cache off | Cache on | Fewer provider calls |",
        "|---|---:|---:|---:|",
    ]
    for workload in ("repeated", "unique"):
        observations = []
        for number in range(1, report["rounds"] + 1):
            pair = sorted(
                (c for c in report["cells"] if c["round"] == number and c["workload"] == workload),
                key=lambda c: c["ttl_seconds"],
            )
            if len(pair) != 2 or pair[0]["ttl_seconds"] != 0 or pair[1]["ttl_seconds"] <= 0:
                raise ValueError(f"Missing control or comparison for {workload}, round {number}")
            baseline, cached = pair
            if baseline["result_sequence_sha256"] != cached["result_sequence_sha256"]:
                raise ValueError(
                    "Can only compare calls when both policies return the same answers"
                )
            observations.append(
                (
                    baseline["tasks"],
                    baseline["distinct_skus"],
                    baseline["destination_posts"],
                    cached["destination_posts"],
                )
            )
        if not observations or len(set(observations)) != 1:
            raise ValueError(
                "Per-round counts differ. Publish their range before updating the table."
            )
        tasks, keys, origin, reduced = observations[0]
        if workload == "unique":
            label = f"{tasks} reads, {keys} different SKUs"
        else:
            label = f"{tasks} reads, {keys} recurring SKUs"
        reduction = f"{100 * (origin - reduced) / origin:.1f}".removesuffix(".0")
        rows.append(f"| {label} | {origin} | {reduced} | {reduction}% |")
    return "\n".join(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--receipt", type=Path, default=DIRECTORY / "results" / "bindings-stable.json"
    )
    parser.add_argument("--check", action="store_true", help="Verify the checked-in README table")
    args = parser.parse_args()
    table = render(json.loads(args.receipt.read_text(encoding="utf-8")))
    if args.check:
        readme = (DIRECTORY.parent / "README.md").read_text(encoding="utf-8")
        if readme.count(START) != 1 or readme.count(END) != 1:
            raise SystemExit("Missing or duplicate benchmark table markers")
        published = readme.split(START, 1)[1].split(END, 1)[0].strip()
        if table != published:
            raise SystemExit(f"README benchmark table differs from receipt. Expected:\n{table}")
    else:
        print(table)


if __name__ == "__main__":
    main()
