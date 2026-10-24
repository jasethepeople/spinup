from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class FactDraft:
    kind: str
    value: str
    source_file: str | None = None
    confidence: float = 0.5


@dataclass(slots=True)
class Detection:
    stack: str
    facts: list[FactDraft] = field(default_factory=list)


@dataclass(slots=True)
class Plan:
    stack: str
    setup: list[str] = field(default_factory=list)
    start: str = ""
    port: int = 8000
    env: dict[str, str] = field(default_factory=dict)
    services: list[dict[str, str]] = field(default_factory=list)
    reason: str = "rule-based plan"

    @classmethod
    def from_dict(cls, data: dict[str, Any], fallback_stack: str = "unknown") -> "Plan":
        if not isinstance(data, dict):
            raise ValueError("plan must be a JSON object")
        stack = str(data.get("stack", fallback_stack))
        setup = data.get("setup", [])
        if isinstance(setup, str):
            setup = [setup]
        if not isinstance(setup, list) or not all(isinstance(x, str) and x.strip() for x in setup):
            raise ValueError("plan setup must be a list of commands")
        start = data.get("start", "")
        if not isinstance(start, str) or not start.strip():
            raise ValueError("plan start must be a non-empty command")
        port = int(data.get("port", 8000))
        if not 1 <= port <= 65535:
            raise ValueError("plan port is outside 1-65535")
        env = data.get("env", {})
        if not isinstance(env, dict):
            raise ValueError("plan env must be an object")
        clean_env = {str(k): str(v) for k, v in env.items() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k))}
        services = data.get("services", [])
        if not isinstance(services, list):
            raise ValueError("plan services must be a list")
        return cls(stack, list(setup), start.strip(), port, clean_env, services, str(data.get("reason", "LLM plan"))[:500])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RunResult:
    status: str
    repo_id: int
    url: str
    commit_sha: str
    attempts: int = 0
    recipe_path: str | None = None
    reason: str | None = None
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
