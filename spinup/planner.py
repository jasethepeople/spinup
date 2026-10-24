from __future__ import annotations

import json
import re
from pathlib import Path

from .llm import LLMError, LLMProvider
from .models import Plan


class UnsupportedRepo(RuntimeError):
    pass


def fake_value(name: str) -> str:
    if name == "PORT":
        return ""
    if any(word in name for word in ("URL", "DSN")):
        return "http://localhost:8000"
    return "spinup-fake-" + name.lower().replace("_", "-")[:40]


class RulePlanner:
    def plan(self, root: Path, detected_stack: str, facts: list[dict]) -> Plan:
        stack = detected_stack
        if stack == "compose":
            if (root / "package.json").exists():
                stack = "node"
            elif any((root / n).exists() for n in ("requirements.txt", "pyproject.toml", "setup.py")):
                stack = "python"
        if stack == "unknown":
            raise UnsupportedRepo("no supported Node, Python, Go, Rust, or Compose manifest")
        ports = [f for f in facts if f["kind"] == "port" and str(f["value"]).isdigit()]
        port = int(max(ports, key=lambda f: (f["confidence"], -f["id"]))["value"]) if ports else 8000
        env = {f["value"]: fake_value(f["value"]) for f in facts if f["kind"] == "env_var"}
        env.update({"HOST": "0.0.0.0", "PORT": str(port)})
        services = []
        allowed = {"postgres": "postgres:16", "redis": "redis:7", "mysql": "mysql:8", "mongo": "mongo:7"}
        for fact in facts:
            if fact["kind"] != "service":
                continue
            try:
                value = json.loads(fact["value"])
                name = str(value.get("name", "")).lower()
            except (json.JSONDecodeError, AttributeError):
                name = str(fact["value"]).lower()
            for key, image in allowed.items():
                if key in name and not any(s["name"] == key for s in services):
                    services.append({"name": key, "image": image})
        if any(s["name"] == "postgres" for s in services):
            env.setdefault("DATABASE_URL", "postgresql://postgres:spinup-fake@postgres:5432/postgres")
        if any(s["name"] == "redis" for s in services):
            env.setdefault("REDIS_URL", "redis://redis:6379/0")

        if stack == "node":
            setup = ["npm ci"] if (root / "package-lock.json").exists() else ["npm install"]
            data = {}
            try:
                data = json.loads((root / "package.json").read_text())
            except (OSError, json.JSONDecodeError):
                pass
            scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
            if "start" in scripts:
                start = "npm start"
            elif "dev" in scripts:
                start = f"npm run dev -- --host 0.0.0.0 --port {port}"
            elif "serve" in scripts:
                start = f"npm run serve -- --host 0.0.0.0 --port {port}"
            else:
                start = "node server.js" if (root / "server.js").exists() else "node index.js"
                if not (root / ("server.js" if start == "node server.js" else "index.js")).exists():
                    raise UnsupportedRepo("Node project has no start/dev/serve script or server.js/index.js")
            return Plan(stack, setup, start, port, env, services)
        if stack == "python":
            setup = ["python -m venv .venv"]
            if (root / "requirements.txt").exists():
                setup.append(".venv/bin/pip install -r requirements.txt")
            elif (root / "pyproject.toml").exists():
                setup.append(".venv/bin/pip install .")
            elif (root / "setup.py").exists():
                setup.append(".venv/bin/pip install .")
            if (root / "manage.py").exists():
                start = f".venv/bin/python manage.py runserver 0.0.0.0:{port}"
            else:
                entry = next((n for n in ("app.py", "main.py", "server.py") if (root / n).exists()), None)
                if not entry:
                    raise UnsupportedRepo("Python project has no app.py, main.py, server.py, or manage.py")
                if (root / "streamlit_app.py").exists():
                    entry = "streamlit_app.py"
                    start = f".venv/bin/streamlit run {entry} --server.address 0.0.0.0 --server.port {port}"
                else:
                    start = f".venv/bin/python {entry}"
            return Plan(stack, setup, start, port, env, services)
        if stack == "go":
            return Plan(stack, ["go build -o spinup-app ."], "./spinup-app", port, env, services)
        if stack == "rust":
            name = "app"
            text = (root / "Cargo.toml").read_text(errors="replace")
            match = re.search(r'(?m)^name\s*=\s*"([^"]+)"', text)
            if match:
                name = match.group(1)
            return Plan(stack, ["cargo build --release"], f"./target/release/{name}", port, env, services)
        raise UnsupportedRepo(f"unsupported stack: {stack}")


class Planner:
    def __init__(self, llm: LLMProvider | None = None):
        self.llm = llm
        self.rules = RulePlanner()

    def initial(self, root: Path, stack: str, facts: list[dict]) -> Plan:
        try:
            return self.rules.plan(root, stack, facts)
        except UnsupportedRepo as rule_error:
            if not self.llm:
                raise
            try:
                data = self.llm.generate_json("planner", {"stack": stack, "facts": facts[-100:]})
                if data.get("unsupported"):
                    raise UnsupportedRepo(str(data["unsupported"]))
                plan = Plan.from_dict(data, stack)
                self._reject_escape_commands(plan)
                return plan
            except LLMError:
                raise rule_error

    def repair(self, plan: Plan, facts: list[dict], lessons: list[dict], log: str) -> Plan:
        if self.llm:
            try:
                data = self.llm.generate_json("repair", {
                    "current_plan": plan.to_dict(), "facts": facts[-100:],
                    "lessons": lessons, "failure_log": log[-3500:],
                })
                if data.get("unsupported"):
                    raise UnsupportedRepo(str(data["unsupported"]))
                repaired = Plan.from_dict(data, plan.stack)
                self._reject_escape_commands(repaired)
                return repaired
            except LLMError:
                pass
        return self._heuristic_repair(plan, facts, log)

    @staticmethod
    def _reject_escape_commands(plan: Plan) -> None:
        commands = " ".join([*plan.setup, plan.start]).lower()
        forbidden = ("docker.sock", "nsenter", "unshare", "mount ", "privileged", "sudo ")
        if any(token in commands for token in forbidden):
            raise UnsupportedRepo("model proposed a command outside the sandbox policy")

    @staticmethod
    def _heuristic_repair(plan: Plan, facts: list[dict], log: str) -> Plan:
        data = plan.to_dict()
        missing_py = re.search(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)", log)
        missing_node = re.search(r"Cannot find module ['\"]([^'\"]+)", log)
        if missing_py:
            module = re.sub(r"[^A-Za-z0-9_.-]", "", missing_py.group(1).split(".")[0])
            if module:
                data["setup"].append(f".venv/bin/pip install {module}")
                data["reason"] = f"install missing module {module}"
        elif missing_node:
            module = missing_node.group(1)
            if re.fullmatch(r"[@A-Za-z0-9_./-]+", module) and not module.startswith((".", "/")):
                data["setup"].append(f"npm install {module}")
                data["reason"] = f"install missing module {module}"
        elif "EADDRINUSE" in log or "address already in use" in log.lower():
            data["port"] = 18000 if plan.port != 18000 else 28000
            data["env"]["PORT"] = str(data["port"])
            data["reason"] = "change occupied port"
        else:
            raise UnsupportedRepo("no verified repair rule and no usable LLM repair response")
        return Plan.from_dict(data, plan.stack)
