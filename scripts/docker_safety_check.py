#!/usr/bin/env python3
"""Manual smoke and isolation checks for the real Docker/Podman backend."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spinup.config import Settings
from spinup.sandbox import DockerSandbox
from spinup.verifier import verify


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    settings = Settings.load(args.root, args.config)
    if settings.runtime not in {"docker", "podman"}:
        parser.error("set runtime to docker or podman")
    settings.sandbox["image"] = settings.sandbox.get("base_images", {}).get("python", "python:3.11-slim")
    checks: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name} {detail}", flush=True)

    sentinel = Path(tempfile.gettempdir()) / f"spinup-host-sentinel-{uuid.uuid4().hex}"
    sentinel.write_text("this host file must not be visible")
    os.environ["SPINUP_REAL_SECRET"] = "must-not-enter-container"
    box = DockerSandbox(settings, "python")
    try:
        box.create(8000)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / ".env").write_text("REAL_SECRET=must-not-be-copied")
            (source / "index.html").write_text("spinup safety check")
            box.copy_in(source)
        record("container creation", True)
        published = box._run(["port", box.name], 10)
        record("no ports published to host", published.returncode == 0 and not published.stdout.strip(), published.stdout[-500:])
        result = box.exec(f"python -c \"from pathlib import Path; raise SystemExit(Path('{sentinel}').exists())\"", 10)
        record("host filesystem is not mounted", result.exit_code == 0, result.output)
        result = box.exec("python -c \"import os; raise SystemExit('SPINUP_REAL_SECRET' in os.environ or 'REAL_SECRET' in os.environ)\"", 10)
        record("host and .env secrets are absent", result.exit_code == 0, result.output)
        result = box.exec("python -c \"import urllib.request; urllib.request.urlopen('https://pypi.org', timeout=10)\"", 20)
        record("allowlisted registry reachable through proxy", result.exit_code == 0, result.output[-500:])
        result = box.exec("python -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)\"", 15)
        record("non-allowlisted domain blocked", result.exit_code != 0, result.output[-500:])
        result = box.exec("python -m http.server 8000 --bind 0.0.0.0", 10, detach=True)
        checked = verify(box, 8000, 15)
        record("process alive; port verified from inside the sandbox", result.exit_code == 0 and checked.ok, checked.reason)
        result = box.exec("sleep 30", 1)
        record("command timeout kills container", result.timed_out and box.destroyed, result.output)
    except Exception as exc:
        record("manual safety harness completed", False, f"{type(exc).__name__}: {exc}")
    finally:
        box.destroy()
        sentinel.unlink(missing_ok=True)
        os.environ.pop("SPINUP_REAL_SECRET", None)
    failed = [name for name, ok, _ in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("Do not run untrusted repositories until every failure is resolved.")
        return 1
    print("All checks passed. The real backend is ready for the small benchmark run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
