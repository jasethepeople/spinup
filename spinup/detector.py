from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Detection, FactDraft


COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


def read_text(path: Path, limit: int = 1_000_000) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def detect(root: Path) -> Detection:
    facts: list[FactDraft] = []
    compose = next((root / name for name in COMPOSE_NAMES if (root / name).is_file()), None)
    package = root / "package.json"
    python_markers = [root / n for n in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile") if (root / n).is_file()]
    go = root / "go.mod"
    rust = root / "Cargo.toml"

    if compose:
        stack = "compose"
        text = read_text(compose)
        facts.append(FactDraft("dependency", "compose", compose.name, 1.0))
        for service in re.findall(r"(?m)^  ([A-Za-z0-9_.-]+):\s*$", text):
            facts.append(FactDraft("service", service, compose.name, 0.9))
        for port in re.findall(r"(?m)^\s*-\s*[\"']?(\d+):(\d+)", text):
            facts.append(FactDraft("port", port[1], compose.name, 0.85))
    elif package.is_file():
        stack = "node"
        facts.append(FactDraft("dependency", "package.json", "package.json", 1.0))
        try:
            data = json.loads(read_text(package))
            scripts = data.get("scripts", {})
            for name in ("start", "dev", "serve", "build"):
                if name in scripts:
                    facts.append(FactDraft("entrypoint", f"npm run {name}", "package.json", 0.9))
            if isinstance(data.get("dependencies"), dict):
                for dep in data["dependencies"]:
                    facts.append(FactDraft("dependency", dep, "package.json", 0.8))
        except (json.JSONDecodeError, AttributeError):
            facts.append(FactDraft("warning", "package.json could not be parsed", "package.json", 1.0))
    elif python_markers:
        stack = "python"
        for marker in python_markers:
            facts.append(FactDraft("dependency", marker.name, marker.name, 1.0))
        for name in ("app.py", "main.py", "server.py", "manage.py"):
            if (root / name).is_file():
                facts.append(FactDraft("entrypoint", name, name, 0.8))
    elif go.is_file():
        stack = "go"
        facts.append(FactDraft("dependency", "go.mod", "go.mod", 1.0))
    elif rust.is_file():
        stack = "rust"
        facts.append(FactDraft("dependency", "Cargo.toml", "Cargo.toml", 1.0))
    else:
        stack = "unknown"
        facts.append(FactDraft("warning", "no supported project manifest found", None, 1.0))

    readme = next((root / n for n in ("README.md", "README.rst", "README") if (root / n).is_file()), None)
    if readme:
        text = read_text(readme, 300_000)
        for match in re.finditer(r"(?i)\b(?:port|localhost:|0\.0\.0\.0:|127\.0\.0\.1:)\s*(\d{2,5})", text):
            port = int(match.group(1))
            if 1 <= port <= 65535:
                facts.append(FactDraft("port", str(port), readme.name, 0.65))
        for command in re.findall(r"(?m)^\s*((?:npm|pnpm|yarn|python|python3|uvicorn|gunicorn|flask|streamlit|go run|cargo run)\s+[^\n`]+)", text):
            facts.append(FactDraft("entrypoint", command.strip(), readme.name, 0.55))
    if not any(f.kind == "port" for f in facts):
        default = {"node": 3000, "python": 8000, "go": 8080, "rust": 8080, "compose": 8000}.get(stack, 8000)
        facts.append(FactDraft("port", str(default), "detector default", 0.3))
    return Detection(stack, facts)
