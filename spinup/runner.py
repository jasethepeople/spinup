from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .analyst import Analyst
from .db import Lane, Memory
from .models import Detection, Plan
from .planner import Planner, UnsupportedRepo
from .sandbox import ExecResult, Sandbox, SandboxError
from .verifier import verify


@dataclass(slots=True)
class RunnerOutcome:
    status: str
    plan: Plan | None
    attempts_used: int
    reason: str


class Runner:
    def __init__(self, memory: Memory, planner: Planner,
                 sandbox_factory: Callable[[Plan], Sandbox], command_timeout: int = 180,
                 verify_timeout: int = 20, analyst: Analyst | None = None):
        self.memory = memory
        self.attempt_memory = memory.lane_for(Lane.RUNNER)
        self.planner = planner
        self.sandbox_factory = sandbox_factory
        self.command_timeout = command_timeout
        self.verify_timeout = verify_timeout
        self.analyst = analyst

    def close(self) -> None:
        self.attempt_memory.close()

    def run(self, repo: dict, source: Path, detection: Detection,
            max_attempts: int = 8, initial_plan: Plan | None = None) -> RunnerOutcome:
        repo_id = int(repo["id"])
        try:
            plan = initial_plan or self.planner.initial(source, detection.stack, self.memory.facts(repo_id))
        except UnsupportedRepo as exc:
            return RunnerOutcome("unsupported", None, 0, str(exc))
        pending_lesson: tuple[str, str] | None = None
        last_reason = "maximum attempts reached"

        for number in range(1, max_attempts + 1):
            sandbox = self.sandbox_factory(plan)
            failure_log = ""
            try:
                try:
                    sandbox.create(plan.port, plan.services)
                    sandbox.copy_in(source)
                except SandboxError as exc:
                    self._record(repo_id, number, "build", "create sandbox", ExecResult(1, str(exc)))
                    return RunnerOutcome("failed", plan, number, str(exc))
                failed = False
                for command in plan.setup:
                    result = sandbox.exec(command, self.command_timeout, plan.env)
                    self._record(repo_id, number, "install", command, result)
                    if result.exit_code != 0 or result.timed_out:
                        failure_log = result.output
                        failed = True
                        break
                if not failed:
                    result = sandbox.exec(plan.start, self.command_timeout, plan.env, detach=True)
                    self._record(repo_id, number, "start", plan.start, result)
                    if result.exit_code == 0 and not result.timed_out:
                        checked = verify(sandbox, plan.port, self.verify_timeout)
                        verification_result = ExecResult(0 if checked.ok else 1, checked.reason)
                        self._record(repo_id, number, "verify", f"alive + port {plan.port}", verification_result)
                        if checked.ok:
                            if pending_lesson:
                                self.memory.learn(plan.stack, *pending_lesson, worked=True)
                            return RunnerOutcome("success", plan, number, checked.reason)
                        failure_log = (sandbox.logs() + "\n" + checked.reason)[-4096:]
                    else:
                        failure_log = result.output
                last_reason = failure_log[-500:] or "attempt failed"
                if number == max_attempts:
                    break
                try:
                    if self.analyst:
                        self.analyst.inspect_failure(source, repo_id, failure_log)
                    repaired = self.planner.repair(
                        plan, self.memory.facts(repo_id), self.memory.lessons(plan.stack), failure_log,
                    )
                    if repaired.to_dict() == plan.to_dict():
                        raise UnsupportedRepo("repair planner returned an unchanged plan")
                    pending_lesson = (self._error_signature(failure_log), repaired.reason)
                    plan = repaired
                except UnsupportedRepo as exc:
                    last_reason = str(exc)
                    if pending_lesson:
                        self.memory.learn(plan.stack, *pending_lesson, worked=False)
                    return RunnerOutcome("unsupported", plan, number, last_reason)
            finally:
                sandbox.destroy()
        if pending_lesson:
            self.memory.learn(plan.stack, *pending_lesson, worked=False)
        return RunnerOutcome("failed", plan, max_attempts, last_reason)

    def _record(self, repo_id: int, number: int, step: str, command: str, result: ExecResult) -> None:
        self.attempt_memory.add_attempt(
            repo_id, number, step, command, result.exit_code, result.output, result.verdict,
        )

    @staticmethod
    def _error_signature(log: str) -> str:
        lines = [line.strip() for line in log.splitlines() if line.strip()]
        return (lines[-1] if lines else "unknown failure")[:500]
