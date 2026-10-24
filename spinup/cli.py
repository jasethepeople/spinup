from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .gitops import CloneError
from .orchestrator import Orchestrator
from .planner import UnsupportedRepo
from .sandbox import SandboxError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="spinup", description="GitHub repo to verified sandbox recipe")
    root.add_argument("--root", default=".", help="project/state directory (default: current directory)")
    root.add_argument("--config", help="path to config.toml")
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="analyze, run, verify, and save a repository recipe")
    run.add_argument("url")
    run.add_argument("--no-analyst", action="store_true", help="use detector facts only")
    run.add_argument("--json", action="store_true")
    for name in ("facts", "attempts"):
        command = sub.add_parser(name, help=f"show recorded {name}")
        command.add_argument("url")
        command.add_argument("--json", action="store_true")
    replay = sub.add_parser("replay", help="re-run a saved verified recipe")
    replay.add_argument("url")
    replay.add_argument("--json", action="store_true")
    sub.add_parser("clean", help="remove clones and leftover SpinUp containers")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = Settings.load(Path(args.root), args.config)
        with Orchestrator(settings) as app:
            if args.command == "run":
                result = app.run(args.url, use_analyst=not args.no_analyst)
                if args.json:
                    print(json.dumps(result.to_dict(), indent=2))
                else:
                    print(f"{result.status.upper()}: {args.url}")
                    print(f"commit: {result.commit_sha}")
                    if result.recipe_path:
                        print(f"recipe: {result.recipe_path}{' (cached)' if result.cached else ''}")
                    if result.reason:
                        print(f"reason: {result.reason}")
                return 0 if result.status == "success" else 2
            if args.command == "replay":
                result = app.replay(args.url)
                print(json.dumps(result.to_dict(), indent=2) if args.json else f"{result.status.upper()}: {result.reason}")
                return 0 if result.status == "success" else 2
            if args.command in {"facts", "attempts"}:
                repo, rows = getattr(app, args.command)(args.url)
                if args.json:
                    print(json.dumps({"repo": repo, args.command: rows}, indent=2))
                else:
                    print(f"{repo['url']} @ {repo['commit_sha']} [{repo['status']}]")
                    for row in rows:
                        if args.command == "facts":
                            print(f"[{row['kind']}] {row['value']} ({row['source_file']}, confidence={row['confidence']})")
                        else:
                            print(f"attempt {row['attempt_no']} {row['step']}: {row['verdict']} exit={row['exit_code']}\n  {row['command']}\n  {row['log_tail'] or ''}")
                return 0
            if args.command == "clean":
                app.clean()
                print("SpinUp clones and leftover containers removed; database and recipes retained.")
                return 0
    except (ValueError, CloneError, SandboxError, UnsupportedRepo, RuntimeError, OSError) as exc:
        print(f"spinup: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
