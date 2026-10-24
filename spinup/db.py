from __future__ import annotations

import json
import sqlite3
from enum import Enum
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from .models import FactDraft


class Lane(str, Enum):
    ORCHESTRATOR = "orchestrator"
    ANALYST = "analyst"
    RUNNER = "runner"


class LaneViolation(PermissionError):
    pass


class Memory:
    """All SQLite access. A connection's lane is enforced by SQLite's authorizer."""

    WRITES = {
        Lane.ORCHESTRATOR: {"repos", "recipes", "lessons"},
        Lane.ANALYST: {"facts"},
        Lane.RUNNER: {"attempts"},
    }

    def __init__(self, path: str | Path, lane: Lane = Lane.ORCHESTRATOR):
        self.path = str(path)
        self.lane = lane
        # Autocommit prevents a rejected statement in one lane from holding a
        # transaction lock against the other lanes.
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        if lane == Lane.ORCHESTRATOR:
            self.conn.executescript(files("spinup").joinpath("schema.sql").read_text())
        self.conn.set_authorizer(self._authorize)

    def _authorize(self, action: int, arg1: str | None, arg2: str | None, *_: Any) -> int:
        write_actions = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
        if action in write_actions:
            table = (arg1 or "").lower()
            if table.startswith("sqlite_"):
                return sqlite3.SQLITE_OK
            if table not in self.WRITES[self.lane]:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_DELETE:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def lane_for(self, lane: Lane) -> "Memory":
        return Memory(self.path, lane)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get_or_create_repo(self, url: str, sha: str, stack: str | None = None) -> dict[str, Any]:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO repos(url, commit_sha, stack) VALUES (?, ?, ?)",
                (url, sha, stack),
            )
        row = self.conn.execute("SELECT * FROM repos WHERE url=? AND commit_sha=?", (url, sha)).fetchone()
        assert row is not None
        return dict(row)

    def get_repo(self, repo_id: int) -> dict[str, Any] | None:
        return self._dict(self.conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone())

    def find_repo(self, url: str, successful: bool = False) -> dict[str, Any] | None:
        sql = "SELECT * FROM repos WHERE url=?"
        if successful:
            sql += " AND status='success'"
        sql += " ORDER BY id DESC LIMIT 1"
        return self._dict(self.conn.execute(sql, (url,)).fetchone())

    def set_stack(self, repo_id: int, stack: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE repos SET stack=? WHERE id=?", (stack, repo_id))

    def set_status(self, repo_id: int, status: str) -> None:
        if status not in {"new", "analyzing", "running", "success", "failed", "unsupported"}:
            raise ValueError(f"invalid status: {status}")
        with self.conn:
            self.conn.execute("UPDATE repos SET status=? WHERE id=?", (status, repo_id))

    def add_fact(self, repo_id: int, fact: FactDraft) -> int:
        if self.lane != Lane.ANALYST:
            raise LaneViolation("facts require the analyst lane")
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO facts(repo_id,kind,value,source_file,confidence) VALUES(?,?,?,?,?)",
                (repo_id, fact.kind, fact.value, fact.source_file, max(0.0, min(1.0, fact.confidence))),
            )
        return int(cursor.lastrowid)

    def add_facts(self, repo_id: int, facts: Iterable[FactDraft]) -> None:
        for fact in facts:
            self.add_fact(repo_id, fact)

    def facts(self, repo_id: int, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self.conn.execute("SELECT * FROM facts WHERE repo_id=? AND kind=? ORDER BY id", (repo_id, kind))
        else:
            rows = self.conn.execute("SELECT * FROM facts WHERE repo_id=? ORDER BY id", (repo_id,))
        return [dict(row) for row in rows]

    def add_attempt(self, repo_id: int, number: int, step: str, command: str,
                    exit_code: int | None, log_tail: str, verdict: str) -> int:
        if self.lane != Lane.RUNNER:
            raise LaneViolation("attempts require the runner lane")
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO attempts(repo_id,attempt_no,step,command,exit_code,log_tail,verdict) VALUES(?,?,?,?,?,?,?)",
                (repo_id, number, step, command, exit_code, log_tail[-4096:], verdict),
            )
        return int(cursor.lastrowid)

    def attempts(self, repo_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM attempts WHERE repo_id=? ORDER BY id", (repo_id,))]

    def save_recipe(self, repo_id: int, dockerfile: str, compose: str | None,
                    run_cmd: str, port: int, env_stubs: dict[str, str], attempts: int) -> None:
        if self.lane != Lane.ORCHESTRATOR:
            raise LaneViolation("recipes require the orchestrator lane")
        with self.conn:
            self.conn.execute(
                "INSERT INTO recipes(repo_id,dockerfile,compose,run_cmd,port,env_stubs,attempts_used) VALUES(?,?,?,?,?,?,?)",
                (repo_id, dockerfile, compose, run_cmd, port, json.dumps(env_stubs, sort_keys=True), attempts),
            )

    def recipe(self, repo_id: int) -> dict[str, Any] | None:
        return self._dict(self.conn.execute("SELECT * FROM recipes WHERE repo_id=?", (repo_id,)).fetchone())

    def learn(self, stack: str, error_pattern: str, fix: str, worked: bool) -> None:
        error_pattern, fix = error_pattern[:500], fix[:1000]
        with self.conn:
            self.conn.execute(
                """INSERT INTO lessons(stack,error_pattern,fix,worked,failed) VALUES(?,?,?,?,?)
                   ON CONFLICT(stack,error_pattern,fix) DO UPDATE SET
                   worked=worked+excluded.worked, failed=failed+excluded.failed""",
                (stack, error_pattern, fix, int(worked), int(not worked)),
            )

    def lessons(self, stack: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM lessons WHERE stack=? ORDER BY worked DESC, failed ASC, id DESC LIMIT 20", (stack,)
        )]
