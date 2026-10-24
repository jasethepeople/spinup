from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from .analyst import Analyst
from .config import Settings
from .db import Lane, Memory
from .detector import detect
from .gitops import GitCloner, parse_github_url
from .llm import provider_from_settings
from .models import Plan, RunResult
from .planner import Planner
from .recipes import export_recipe, load_recipe
from .runner import Runner
from .sandbox import DockerSandbox, Sandbox, clean_runtime


class Orchestrator:
    def __init__(self, settings: Settings, cloner: GitCloner | None = None,
                 sandbox_factory: Callable[[Plan], Sandbox] | None = None,
                 allow_mock: bool = False):
        self.settings = settings
        self.memory = Memory(settings.database)
        self.cloner = cloner or GitCloner(settings.clone_timeout_seconds)
        self.llm = provider_from_settings(settings.llm)
        self.sandbox_factory = sandbox_factory or self._default_sandbox
        self.allow_mock = allow_mock

    def close(self) -> None:
        self.memory.close()

    def __enter__(self) -> "Orchestrator":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _default_sandbox(self, plan: Plan) -> Sandbox:
        if self.settings.runtime == "mock":
            if not self.allow_mock:
                raise RuntimeError("mock runtime is test-only and cannot verify real repositories")
            from .sandbox import MockSandbox
            return MockSandbox()
        return DockerSandbox(self.settings, plan.stack)

    def run(self, url: str, use_analyst: bool = True) -> RunResult:
        target = parse_github_url(url)
        sha = self.cloner.latest_sha(target.url)
        repo = self.memory.get_or_create_repo(target.url, sha)
        existing = self.memory.recipe(repo["id"])
        if repo["status"] == "success" and existing:
            path = self._recipe_path(target.slug, sha)
            return RunResult("success", repo["id"], target.url, sha, existing["attempts_used"], str(path), cached=True)
        run_dir, analyze_dir = self.cloner.clone_pair(target.url, self.settings.work / str(repo["id"]), sha)
        self.memory.set_status(repo["id"], "analyzing")
        analyst = Analyst(self.memory)
        try:
            if use_analyst:
                detection = analyst.initial(analyze_dir, repo["id"])
            else:
                detection = detect(analyze_dir)
                analyst.memory.add_facts(repo["id"], detection.facts)
            self.memory.set_stack(repo["id"], detection.stack)
            repo = self.memory.get_repo(repo["id"]) or repo
            unsupported = analyst.unsupported_reason(self.memory.facts(repo["id"]))
            if unsupported:
                self.memory.set_status(repo["id"], "unsupported")
                return RunResult("unsupported", repo["id"], target.url, sha, reason=unsupported)
            self.memory.set_status(repo["id"], "running")
            runner = Runner(
                self.memory, Planner(self.llm), self.sandbox_factory,
                self.settings.command_timeout, self.settings.verify_timeout,
                analyst if use_analyst else None,
            )
            try:
                outcome = runner.run(repo, run_dir, detection, self.settings.max_attempts)
            finally:
                runner.close()
            if outcome.status != "success" or not outcome.plan:
                self.memory.set_status(repo["id"], outcome.status)
                return RunResult(outcome.status, repo["id"], target.url, sha, outcome.attempts_used, reason=outcome.reason)
            attempts = self.memory.attempts(repo["id"])
            path, dockerfile, compose = export_recipe(
                self.settings.recipes, target.slug, sha, outcome.plan,
                self.settings.image_for(outcome.plan.stack), attempts,
            )
            self.memory.save_recipe(
                repo["id"], dockerfile, compose, outcome.plan.start,
                outcome.plan.port, outcome.plan.env, outcome.attempts_used,
            )
            self.memory.set_status(repo["id"], "success")
            return RunResult("success", repo["id"], target.url, sha, outcome.attempts_used, str(path))
        finally:
            analyst.close()

    def replay(self, url: str) -> RunResult:
        target = parse_github_url(url)
        repo = self.memory.find_repo(target.url, successful=True)
        if not repo or not self.memory.recipe(repo["id"]):
            raise ValueError("no verified recipe exists for this repository")
        path = self._recipe_path(target.slug, repo["commit_sha"])
        plan = load_recipe(path)
        source = self.settings.work / str(repo["id"]) / "replay"
        self.cloner.clone_commit(target.url, source, repo["commit_sha"])
        self.memory.set_status(repo["id"], "running")
        runner = Runner(self.memory, Planner(self.llm), self.sandbox_factory,
                        self.settings.command_timeout, self.settings.verify_timeout)
        try:
            detection = detect(source)
            outcome = runner.run(repo, source, detection, max_attempts=1, initial_plan=plan)
        finally:
            runner.close()
        self.memory.set_status(repo["id"], "success" if outcome.status == "success" else "failed")
        return RunResult(outcome.status, repo["id"], target.url, repo["commit_sha"],
                         outcome.attempts_used, str(path) if outcome.status == "success" else None,
                         outcome.reason)

    def _recipe_path(self, slug: str, sha: str) -> Path:
        return self.settings.recipes / f"{slug}-{sha[:12]}"

    def facts(self, url: str) -> tuple[dict, list[dict]]:
        target = parse_github_url(url)
        repo = self.memory.find_repo(target.url)
        if not repo:
            raise ValueError("repository has not been run")
        return repo, self.memory.facts(repo["id"])

    def attempts(self, url: str) -> tuple[dict, list[dict]]:
        target = parse_github_url(url)
        repo = self.memory.find_repo(target.url)
        if not repo:
            raise ValueError("repository has not been run")
        return repo, self.memory.attempts(repo["id"])

    def clean(self) -> None:
        if self.settings.work.exists():
            for child in self.settings.work.iterdir():
                if child.name == ".gitkeep":
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        if self.settings.runtime in {"docker", "podman"}:
            clean_runtime(self.settings.runtime)
