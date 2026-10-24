from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class CloneError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitHubURL:
    owner: str
    repo: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{self.owner}-{self.repo}")


def parse_github_url(value: str) -> GitHubURL:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        raise ValueError("only HTTPS GitHub repository URLs are accepted")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port:
        raise ValueError("credentials, ports, query strings, and fragments are not accepted")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 2:
        raise ValueError("use https://github.com/owner/repository")
    owner, repo = parts
    repo = repo.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid GitHub owner or repository name")
    return GitHubURL(owner, repo)


class GitCloner:
    def __init__(self, timeout: int = 120):
        self.timeout = timeout

    @staticmethod
    def _env() -> dict[str, str]:
        env = dict(os.environ)
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        })
        return env

    def _git(self, args: list[str], cwd: Path | None = None) -> str:
        command = ["git", "-c", "credential.helper=", *args]
        try:
            result = subprocess.run(
                command, cwd=cwd, env=self._env(), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=self.timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CloneError(f"git failed: {exc}") from exc
        if result.returncode != 0:
            raise CloneError(result.stdout[-4000:])
        return result.stdout.strip()

    def latest_sha(self, url: str) -> str:
        output = self._git(["ls-remote", url, "HEAD"])
        sha = output.split()[0] if output else ""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise CloneError("GitHub did not return a valid HEAD commit")
        return sha.lower()

    def clone(self, url: str, destination: Path, expected_sha: str) -> None:
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._git([
            "clone", "--depth", "1", "--no-tags", "--single-branch",
            "--no-recurse-submodules", url, str(destination),
        ])
        actual = self._git(["rev-parse", "HEAD"], destination).lower()
        if actual != expected_sha:
            shutil.rmtree(destination, ignore_errors=True)
            raise CloneError("repository HEAD changed during cloning; run again")

    def clone_pair(self, url: str, base: Path, sha: str) -> tuple[Path, Path]:
        run, analyze = base / "run", base / "analyze"
        self.clone(url, run, sha)
        try:
            self.clone(url, analyze, sha)
        except Exception:
            shutil.rmtree(run, ignore_errors=True)
            raise
        return run, analyze

    def clone_commit(self, url: str, destination: Path, sha: str) -> None:
        """Clone an exact saved commit, even when the repository HEAD has moved."""
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        self._git(["init", "--quiet"], destination)
        self._git(["remote", "add", "origin", url], destination)
        self._git(["fetch", "--depth", "1", "--no-tags", "origin", sha], destination)
        self._git(["checkout", "--detach", "FETCH_HEAD"], destination)
        actual = self._git(["rev-parse", "HEAD"], destination).lower()
        if actual != sha:
            raise CloneError("checked out commit does not match the verified recipe")
