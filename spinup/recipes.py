from __future__ import annotations

import json
import shutil
from pathlib import Path

from .models import Plan


def render_dockerfile(plan: Plan, image: str) -> str:
    lines = [
        f"FROM {image}", "WORKDIR /workspace", "COPY . /workspace/",
        "RUN chown -R 1000:1000 /workspace", "USER 1000:1000",
    ]
    for key, value in sorted(plan.env.items()):
        lines.append(f"ENV {key}={json.dumps(value)}")
    for command in plan.setup:
        lines.append(f"RUN {command}")
    lines.extend([f"EXPOSE {plan.port}", "STOPSIGNAL SIGTERM", f'CMD ["sh", "-lc", {json.dumps(plan.start)}]'])
    return "\n".join(lines) + "\n"


def render_compose(plan: Plan) -> str:
    lines = [
        "services:", "  app:", "    build: .", "    read_only: true",
        "    cap_drop: [ALL]", "    security_opt: [no-new-privileges]",
        "    mem_limit: 1g", "    cpus: 1.0", "    pids_limit: 256",
        "    tmpfs:", "      - /workspace:rw,exec,nosuid,size=1024m", "      - /tmp:rw,noexec,nosuid,size=256m",
        "    ports:",
        f'      - "127.0.0.1:{plan.port}:{plan.port}"',
        "    environment:",
    ]
    for key, value in sorted(plan.env.items()):
        lines.append(f'      {key}: {json.dumps(value)}')
    lines.extend(["    networks: [spinup]"])
    for service in plan.services:
        name, image = service["name"], service["image"]
        lines.extend([
            f"  {name}:", f"    image: {image}", "    read_only: true",
            "    cap_drop: [ALL]", "    security_opt: [no-new-privileges]",
            "    mem_limit: 512m", "    networks: [spinup]",
        ])
        if name == "postgres":
            lines.extend(["    environment:", "      POSTGRES_PASSWORD: spinup-fake", "      POSTGRES_DB: postgres"])
    lines.extend(["networks:", "  spinup:", "    internal: true"])
    return "\n".join(lines) + "\n"


def export_recipe(base: Path, slug: str, sha: str, plan: Plan, image: str,
                  attempts: list[dict]) -> tuple[Path, str, str]:
    path = base / f"{slug}-{sha[:12]}"
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    dockerfile, compose = render_dockerfile(plan, image), render_compose(plan)
    (path / "Dockerfile").write_text(dockerfile)
    (path / "compose.yaml").write_text(compose)
    (path / "recipe.json").write_text(json.dumps({"version": 1, "plan": plan.to_dict(), "image": image}, indent=2) + "\n")
    log = "\n".join(
        f"attempt={a['attempt_no']} step={a['step']} verdict={a['verdict']} exit={a['exit_code']}\n"
        f"command={a['command']}\n{a['log_tail'] or ''}" for a in attempts
    ) + "\n"
    (path / "run.log").write_text(log)
    for name in ("Dockerfile", "compose.yaml", "recipe.json", "run.log"):
        if not (path / name).is_file():
            raise OSError(f"recipe export missing {name}")
    return path, dockerfile, compose


def load_recipe(path: Path) -> Plan:
    data = json.loads((path / "recipe.json").read_text())
    if data.get("version") != 1:
        raise ValueError("unsupported recipe version")
    return Plan.from_dict(data["plan"])
