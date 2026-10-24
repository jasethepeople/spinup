#!/usr/bin/env python3
"""Measure real-container success with and without the static Analyst."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spinup.config import Settings
from spinup.orchestrator import Orchestrator


def urls_from(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos", type=Path, default=Path(__file__).parents[1] / "tests" / "repos.txt")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--mode", choices=("both", "analyst", "detector"), default="both")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results"))
    args = parser.parse_args()
    urls = urls_from(args.repos)[: max(1, args.limit)]
    modes = ("analyst", "detector") if args.mode == "both" else (args.mode,)
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {"modes": {}, "warning": "These are real repository runs; execute only after docker_safety_check.py passes."}

    for mode in modes:
        root = args.output.resolve() / mode
        settings = Settings.load(root, args.config)
        results = []
        print(f"\n=== {mode} ===", flush=True)
        for index, url in enumerate(urls, 1):
            started = time.monotonic()
            try:
                with Orchestrator(settings) as app:
                    outcome = app.run(url, use_analyst=mode == "analyst")
                row = outcome.to_dict()
            except Exception as exc:  # benchmark must continue and report every repository
                row = {"url": url, "status": "error", "reason": f"{type(exc).__name__}: {exc}"}
            row["seconds"] = round(time.monotonic() - started, 2)
            results.append(row)
            print(f"[{index}/{len(urls)}] {row['status']:11} {url} ({row['seconds']}s)", flush=True)
        successes = sum(row["status"] == "success" for row in results)
        summary["modes"][mode] = {
            "successes": successes, "total": len(results),
            "success_rate": round(successes / len(results), 4) if results else 0,
            "results": results,
        }
        print(f"{mode}: {successes}/{len(results)} = {summary['modes'][mode]['success_rate']:.1%}")
    if len(modes) == 2:
        a = summary["modes"]["analyst"]["success_rate"]
        d = summary["modes"]["detector"]["success_rate"]
        summary["analyst_advantage_percentage_points"] = round((a - d) * 100, 2)
        print(f"Analyst advantage: {summary['analyst_advantage_percentage_points']:+.2f} percentage points")
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"Saved {args.output / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
