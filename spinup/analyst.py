from __future__ import annotations

import json
import re
from pathlib import Path

from .db import Lane, Memory
from .detector import COMPOSE_NAMES, detect, read_text
from .models import Detection, FactDraft


SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "target", "__pycache__"}
TEXT_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".go", ".rs", ".toml", ".yaml", ".yml", ".json", ".md", ".txt", ".ini", ".cfg", ".example"}
PAID_SECRETS = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS"}


class Analyst:
    """Static, read-only repository analysis. No import, subprocess, or repo execution."""

    def __init__(self, memory: Memory):
        self.memory = memory.lane_for(Lane.ANALYST)

    def close(self) -> None:
        self.memory.close()

    def _write_unique(self, repo_id: int, fact: FactDraft) -> None:
        existing = {(f["kind"], f["value"], f["source_file"]) for f in self.memory.facts(repo_id)}
        if (fact.kind, fact.value, fact.source_file) not in existing:
            self.memory.add_fact(repo_id, fact)

    def initial(self, root: Path, repo_id: int) -> Detection:
        detection = detect(root)
        for fact in detection.facts:
            self._write_unique(repo_id, fact)
        for fact in self.scan(root):
            self._write_unique(repo_id, fact)
        return detection

    def scan(self, root: Path) -> list[FactDraft]:
        facts: list[FactDraft] = []
        seen_env: set[str] = set()
        count = 0
        for path in root.rglob("*"):
            if count >= 500:
                facts.append(FactDraft("warning", "static scan reached the 500-file limit", None, 1.0))
                break
            if path.is_symlink() or not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {".env.example", "Dockerfile"}:
                continue
            try:
                if path.stat().st_size > 500_000:
                    continue
            except OSError:
                continue
            count += 1
            relative = str(path.relative_to(root))
            text = read_text(path, 500_000)
            patterns = [
                r"os\.environ(?:\.get|\[)\(?[\"']([A-Z_][A-Z0-9_]*)",
                r"os\.getenv\([\"']([A-Z_][A-Z0-9_]*)",
                r"process\.env\.([A-Z_][A-Z0-9_]*)",
                r"std::env::var\([\"']([A-Z_][A-Z0-9_]*)",
                r"os\.Getenv\([\"']([A-Z_][A-Z0-9_]*)",
                r"(?m)^([A-Z_][A-Z0-9_]*)=",
            ]
            for pattern in patterns:
                for name in re.findall(pattern, text):
                    if name in seen_env:
                        continue
                    seen_env.add(name)
                    facts.append(FactDraft("env_var", name, relative, 0.85))
                    if name in PAID_SECRETS:
                        facts.append(FactDraft("needs_secret", name, relative, 0.95))
            for port in re.findall(r"(?i)(?:PORT\s*[=:]\s*|listen\(|--port[= ]+|EXPOSE\s+)(\d{2,5})", text):
                if 1 <= int(port) <= 65535:
                    facts.append(FactDraft("port", port, relative, 0.8))
            lowered = text.lower()
            if any(marker in lowered for marker in ("nvidia-smi", "nvidia.com/gpu", "requires gpu", "cuda.is_available()")):
                facts.append(FactDraft("warning", "GPU or CUDA requirement detected", relative, 0.95))
        facts.extend(self._compose_services(root))
        return facts

    def _compose_services(self, root: Path) -> list[FactDraft]:
        facts: list[FactDraft] = []
        for name in COMPOSE_NAMES:
            path = root / name
            if not path.is_file():
                continue
            text = read_text(path)
            blocks = re.split(r"(?m)^  (?=[A-Za-z0-9_.-]+:\s*$)", text)
            for block in blocks[1:]:
                service = block.split(":", 1)[0].strip()
                image = re.search(r"(?m)^\s+image:\s*[\"']?([^\s\"']+)", block)
                value = json.dumps({"name": service, "image": image.group(1) if image else ""})
                facts.append(FactDraft("service", value, name, 0.9))
        return facts

    def inspect_failure(self, root: Path, repo_id: int, log: str) -> list[FactDraft]:
        """Translate recognizable failures into new static/repair facts."""
        discoveries: list[FactDraft] = []
        rules = [
            (r"ModuleNotFoundError: No module named ['\"]([^'\"]+)", "dependency", "missing Python module: {0}"),
            (r"Cannot find module ['\"]([^'\"]+)", "dependency", "missing Node module: {0}"),
            (r"(?i)address already in use|EADDRINUSE", "warning", "selected port is already in use"),
            (r"(?i)permission denied|EACCES", "warning", "permission failure inside sandbox"),
            (r"(?i)connection refused.*(?:5432|postgres)", "service", '{"name":"postgres","image":"postgres:16"}'),
            (r"(?i)connection refused.*(?:6379|redis)", "service", '{"name":"redis","image":"redis:7"}'),
        ]
        for pattern, kind, template in rules:
            match = re.search(pattern, log)
            if match:
                value = template.format(match.group(1)) if "{0}" in template else template
                discoveries.append(FactDraft(kind, value, "failure log", 0.8))
        for name in PAID_SECRETS:
            if name in log:
                discoveries.append(FactDraft("needs_secret", name, "failure log", 0.95))
        for fact in discoveries:
            self._write_unique(repo_id, fact)
        return discoveries

    @staticmethod
    def unsupported_reason(facts: list[dict]) -> str | None:
        secrets = sorted({f["value"] for f in facts if f["kind"] == "needs_secret"})
        if secrets:
            return "real credentials appear necessary: " + ", ".join(secrets)
        gpu = [f for f in facts if f["kind"] == "warning" and "GPU or CUDA" in f["value"]]
        if gpu:
            return "GPU/CUDA hardware is required"
        return None
