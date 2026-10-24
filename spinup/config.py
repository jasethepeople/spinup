from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_IMAGES = {
    "python": "python:3.11-slim",
    "node": "node:20-bookworm-slim",
    "go": "golang:1.22-bookworm",
    "rust": "rust:1-bookworm",
    "compose": "python:3.11-slim",
    "unknown": "python:3.11-slim",
}
DEFAULT_ALLOWLIST = [
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
    "github.com", "api.github.com", "proxy.golang.org", "sum.golang.org",
    "crates.io", "static.crates.io", "index.crates.io",
]


@dataclass(slots=True)
class Settings:
    root: Path
    runtime: str = "docker"
    max_attempts: int = 8
    clone_timeout_seconds: int = 120
    command_timeout_seconds: int = 180
    verify_timeout_seconds: int = 20
    sandbox: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)
    work: Path = Path("work")
    recipes: Path = Path("recipes")
    database: Path = Path("spinup.db")

    @classmethod
    def load(cls, root: str | Path, config_path: str | Path | None = None) -> "Settings":
        root = Path(root).expanduser().resolve()
        path = Path(config_path).expanduser().resolve() if config_path else root / "config.toml"
        data: dict[str, Any] = {}
        if path.exists():
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        paths = data.get("paths", {})
        settings = cls(
            root=root,
            runtime=os.environ.get("SPINUP_RUNTIME", data.get("runtime", "docker")),
            max_attempts=max(1, min(8, int(data.get("max_attempts", 8)))),
            clone_timeout_seconds=int(data.get("clone_timeout_seconds", 120)),
            command_timeout_seconds=int(data.get("command_timeout_seconds", 180)),
            verify_timeout_seconds=int(data.get("verify_timeout_seconds", 20)),
            sandbox=data.get("sandbox", {}),
            llm=data.get("llm", {}),
            work=cls._resolve(root, paths.get("work", "work")),
            recipes=cls._resolve(root, paths.get("recipes", "recipes")),
            database=cls._resolve(root, os.environ.get("SPINUP_DB", paths.get("database", "spinup.db"))),
        )
        if settings.runtime not in {"docker", "podman", "mock"}:
            raise ValueError("runtime must be docker, podman, or mock")
        settings.work.mkdir(parents=True, exist_ok=True)
        settings.recipes.mkdir(parents=True, exist_ok=True)
        settings.database.parent.mkdir(parents=True, exist_ok=True)
        return settings

    @staticmethod
    def _resolve(root: Path, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return (root / path).resolve() if not path.is_absolute() else path.resolve()

    def image_for(self, stack: str) -> str:
        return self.sandbox.get("image") or self.sandbox.get("base_images", {}).get(
            stack, DEFAULT_IMAGES.get(stack, DEFAULT_IMAGES["unknown"])
        )

    @property
    def allowlist(self) -> list[str]:
        return list(self.sandbox.get("network_allowlist", DEFAULT_ALLOWLIST))

    @property
    def command_timeout(self) -> int:
        return self.command_timeout_seconds

    @property
    def verify_timeout(self) -> int:
        return self.verify_timeout_seconds
