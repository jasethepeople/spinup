from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .config import Settings


class SandboxError(RuntimeError):
    pass


@dataclass(slots=True)
class ExecResult:
    exit_code: int
    output: str = ""
    timed_out: bool = False

    @property
    def verdict(self) -> str:
        return "timeout" if self.timed_out else ("ok" if self.exit_code == 0 else "fail")


class Sandbox(ABC):
    @abstractmethod
    def create(self, port: int, services: list[dict[str, str]] | None = None) -> None: ...

    @abstractmethod
    def copy_in(self, source: Path) -> None: ...

    @abstractmethod
    def exec(self, command: str, timeout: int, env: dict[str, str] | None = None,
             detach: bool = False) -> ExecResult: ...

    @abstractmethod
    def logs(self) -> str: ...

    @abstractmethod
    def process_alive(self) -> bool: ...

    @abstractmethod
    def port_answers(self, port: int, timeout: float = 2.0) -> bool: ...

    @abstractmethod
    def destroy(self) -> None: ...


class MockSandbox(Sandbox):
    """Scriptable test double. It never executes repository commands."""

    def __init__(self, responses: list[tuple[str, int, str, bool]] | None = None,
                 port_open: bool = True):
        self.responses = responses or []
        self.port_open = port_open
        self.commands: list[str] = []
        self.created = False
        self.destroyed = False
        self.alive = False
        self.copied_from: Path | None = None
        self._logs = ""

    def create(self, port: int, services: list[dict[str, str]] | None = None) -> None:
        self.created, self.destroyed, self.alive = True, False, False

    def copy_in(self, source: Path) -> None:
        if not self.created or self.destroyed:
            raise SandboxError("mock container is not created")
        self.copied_from = source

    def exec(self, command: str, timeout: int, env: dict[str, str] | None = None,
             detach: bool = False) -> ExecResult:
        if self.destroyed:
            return ExecResult(1, "container destroyed")
        self.commands.append(command)
        result = ExecResult(0, "")
        for pattern, code, output, timed_out in self.responses:
            if re.search(pattern, command):
                result = ExecResult(code, output, timed_out)
                self._logs = (self._logs + "\n" + output)[-4096:]
                self.responses.remove((pattern, code, output, timed_out))
                break
        if detach:
            self.alive = result.exit_code == 0 and not result.timed_out
        if result.timed_out:
            self.alive = False
        return result

    def logs(self) -> str:
        return self._logs

    def process_alive(self) -> bool:
        return self.created and not self.destroyed and self.alive

    def port_answers(self, port: int, timeout: float = 2.0) -> bool:
        return self.process_alive() and self.port_open

    def destroy(self) -> None:
        self.destroyed, self.alive = True, False


class DockerSandbox(Sandbox):
    """Docker/Podman backend with no mounts, an internal app network, and proxy allowlisting."""

    def __init__(self, settings: Settings, stack: str):
        self.settings = settings
        self.runtime = settings.runtime
        if self.runtime not in {"docker", "podman"}:
            raise SandboxError("DockerSandbox requires docker or podman runtime")
        self.stack = stack
        self.id = uuid.uuid4().hex[:12]
        self.name = f"spinup-app-{self.id}"
        self.proxy = f"spinup-proxy-{self.id}"
        self.network = f"spinup-net-{self.id}"
        self.service_names: list[str] = []
        self.app_port: int | None = None
        self.destroyed = False
        self.timeout = settings.command_timeout

    def _run(self, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.runtime, *args], text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=timeout or self.timeout, check=False,
            )
        except FileNotFoundError as exc:
            raise SandboxError(f"{self.runtime} not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"{self.runtime} command timed out") from exc

    def _run_stdin(self, args: list[str], stdin: BinaryIO,
                   timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        """Run an engine command with a streaming stdin file descriptor."""
        try:
            return subprocess.run(
                [self.runtime, *args], stdin=stdin, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout or self.timeout, check=False,
            )
        except FileNotFoundError as exc:
            raise SandboxError(f"{self.runtime} not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"{self.runtime} command timed out") from exc

    def _check(self, args: list[str], timeout: int | None = None) -> str:
        result = self._run(args, timeout)
        if result.returncode != 0:
            raise SandboxError(result.stdout[-4000:])
        return result.stdout.strip()

    @staticmethod
    def _limits(settings: Settings) -> list[str]:
        box = settings.sandbox
        workspace_mb = int(box.get("workspace_mb", 1024))
        tmp_mb = int(box.get("tmp_mb", 256))
        return [
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--cpus", str(box.get("cpu", "1.0")),
            "--memory", str(box.get("memory", "1g")),
            "--pids-limit", str(int(box.get("pids", 256))),
            "--read-only",
            "--tmpfs", f"/workspace:rw,exec,nosuid,size={workspace_mb}m",
            "--tmpfs", f"/tmp:rw,noexec,nosuid,size={tmp_mb}m",
            "--tmpfs", f"/home/app:rw,exec,nosuid,size={tmp_mb}m",
        ]

    def create(self, port: int, services: list[dict[str, str]] | None = None) -> None:
        if shutil.which(self.runtime) is None:
            raise SandboxError(f"{self.runtime} not found")
        self._check(["info", "--format", "{{.ServerVersion}}"], 15)
        self._check(["network", "create", "--internal", "--label", "spinup=1", self.network])
        try:
            self._create_proxy()
            for service in services or []:
                self._create_service(service)
            args = [
                "create", "--name", self.name, "--label", "spinup=1",
                "--network", self.network, "--network-alias", "app",
                "--env", "HOME=/home/app", "--env", "PATH=/usr/local/bin:/usr/bin:/bin",
                "--env", "HTTP_PROXY=http://spinup-proxy:3128",
                "--env", "HTTPS_PROXY=http://spinup-proxy:3128",
                "--env", "http_proxy=http://spinup-proxy:3128",
                "--env", "https_proxy=http://spinup-proxy:3128",
                "--env", "NO_PROXY=localhost,127.0.0.1,app,postgres,redis,mysql,mongo",
                *self._limits(self.settings),
                self.settings.image_for(self.stack), "sleep", "infinity",
            ]
            self._check(args, self.settings.clone_timeout_seconds)
            self._check(["start", self.name])
            self.app_port = port
        except Exception:
            self.destroy()
            raise

    def _create_proxy(self) -> None:
        lines = [
            "http_port 3128", "acl SSL_ports port 443", "acl CONNECT method CONNECT",
            "http_access deny CONNECT !SSL_ports",
        ]
        for domain in self.settings.allowlist:
            clean = domain.strip().lstrip(".")
            if re.fullmatch(r"[A-Za-z0-9.-]+", clean):
                lines.append(f"acl allowed_sites dstdomain .{clean}")
        lines.extend(["http_access allow allowed_sites", "http_access deny all", "access_log none", "cache_log /dev/null"])
        self._check([
            "create", "--name", self.proxy, "--label", "spinup=1",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "256m", "--cpus", "0.5", "--pids-limit", "128",
            self.settings.sandbox.get("squid_image", "ubuntu/squid:6.6-24.04_beta"),
        ], self.settings.clone_timeout_seconds)
        with tempfile.TemporaryDirectory(prefix="spinup-proxy-") as tmp:
            config = Path(tmp) / "squid.conf"
            config.write_text("\n".join(lines) + "\n")
            self._check(["cp", str(config), f"{self.proxy}:/etc/squid/squid.conf"])
        self._check(["start", self.proxy])
        self._check(["network", "connect", "--alias", "spinup-proxy", self.network, self.proxy])

    def _create_service(self, service: dict[str, str]) -> None:
        name, image = service.get("name", ""), service.get("image", "")
        allowed = {
            "postgres": ("postgres:16", "/var/lib/postgresql/data", ["POSTGRES_PASSWORD=spinup-fake", "POSTGRES_DB=postgres"]),
            "redis": ("redis:7", "/data", []),
            "mysql": ("mysql:8", "/var/lib/mysql", ["MYSQL_ROOT_PASSWORD=spinup-fake", "MYSQL_DATABASE=app"]),
            "mongo": ("mongo:7", "/data/db", []),
        }
        if name not in allowed or image != allowed[name][0]:
            raise SandboxError(f"unsupported companion service: {name or image}")
        container = f"spinup-{name}-{self.id}"
        _, data_path, env = allowed[name]
        args = [
            "create", "--name", container, "--label", "spinup=1",
            "--network", self.network, "--network-alias", name,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "512m", "--cpus", "0.5", "--pids-limit", "128", "--read-only",
            "--tmpfs", f"{data_path}:rw,nosuid,size=512m", "--tmpfs", "/tmp:rw,nosuid,size=64m",
        ]
        for item in env:
            args.extend(["--env", item])
        args.append(image)
        self._check(args, self.settings.clone_timeout_seconds)
        self._check(["start", container])
        self.service_names.append(container)

    @staticmethod
    def _safe_copy(source: Path, destination: Path) -> None:
        sensitive = {".env", ".env.local", ".env.production", "credentials.json", "service-account.json"}
        def ignore(directory: str, names: list[str]) -> set[str]:
            # Never follow repository symlinks: they could point into host files.
            return {n for n in names if (Path(directory) / n).is_symlink() or n == ".git" or n in sensitive or n.endswith((".pem", ".key", ".p12")) or n.startswith("id_rsa")}
        shutil.copytree(source, destination, ignore=ignore, symlinks=True)

    def copy_in(self, source: Path) -> None:
        """Stream the sanitized tree into the writable /workspace tmpfs.

        docker cp rejects a read-only container rootfs on some daemons. A tar
        stream through exec stdin writes only to the tmpfs and never buffers the
        repository archive in memory. The proxy's separate docker cp is untouched.
        """
        with tempfile.TemporaryDirectory(prefix="spinup-copy-") as tmp:
            clean = Path(tmp) / "source"
            self._safe_copy(source, clean)
            with tempfile.TemporaryFile(prefix="spinup-tar-errors-") as tar_errors:
                try:
                    tar_process = subprocess.Popen(
                        ["tar", "-cf", "-", "-C", str(clean), "."],
                        stdout=subprocess.PIPE, stderr=tar_errors,
                    )
                except OSError as exc:
                    raise SandboxError(f"could not start host tar: {exc}") from exc
                assert tar_process.stdout is not None
                try:
                    result = self._run_stdin(
                        ["exec", "-i", self.name, "tar", "-x", "-C", "/workspace"],
                        tar_process.stdout, self.settings.clone_timeout_seconds,
                    )
                finally:
                    tar_process.stdout.close()
                    try:
                        tar_process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        tar_process.kill()
                        tar_process.wait()
                tar_errors.seek(0)
                tar_detail = tar_errors.read().decode(errors="replace")[-4000:]
                if tar_process.returncode != 0:
                    raise SandboxError(f"host tar failed: {tar_detail or tar_process.returncode}")
                if result.returncode != 0:
                    raise SandboxError(f"container tar extraction failed: {result.stdout[-4000:]}")
        self._check(["exec", "--user", "root", self.name, "chown", "-R", "1000:1000", "/workspace", "/home/app"])

    def exec(self, command: str, timeout: int, env: dict[str, str] | None = None,
             detach: bool = False) -> ExecResult:
        if self.destroyed:
            return ExecResult(1, "container destroyed")
        args = ["exec", "--user", "1000:1000", "--workdir", "/workspace"]
        for key, value in (env or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            args.extend(["--env", f"{key}={value}"])
        if detach:
            args.append("--detach")
            wrapped = f"nohup sh -c {shlex.quote(command)} > /tmp/spinup-app.log 2>&1 & echo $! > /tmp/spinup-app.pid"
        else:
            wrapped = command
        args.extend([self.name, "sh", "-lc", wrapped])
        try:
            result = self._run(args, timeout)
            return ExecResult(result.returncode, result.stdout[-4096:])
        except SandboxError as exc:
            if "timed out" in str(exc):
                self.destroy()
                return ExecResult(124, f"timeout after {timeout}s", True)
            raise

    def logs(self) -> str:
        app = self._run(["exec", "--user", "root", self.name, "sh", "-c", "cat /tmp/spinup-app.log 2>/dev/null || true"], 10)
        engine = self._run(["logs", "--tail", "100", self.name], 10)
        return (app.stdout + engine.stdout)[-4096:]

    def process_alive(self) -> bool:
        if self.destroyed:
            return False
        state = self._run(["inspect", "--format", "{{.State.Running}}", self.name], 10)
        if state.returncode or state.stdout.strip() != "true":
            return False
        result = self._run(["exec", "--user", "root", self.name, "sh", "-c", 'test -s /tmp/spinup-app.pid && kill -0 "$(cat /tmp/spinup-app.pid)"'], 10)
        return result.returncode == 0

    def port_answers(self, port: int, timeout: float = 2.0) -> bool:
        """Probe the app loopback interface from inside its own network namespace."""
        if self.destroyed:
            return False
        connect_timeout = max(0.1, min(float(timeout), 2.0))
        seconds = max(1, int(timeout))
        # The first available client is used. Only when none exists do we inspect
        # /proc for a LISTEN socket. All probe work happens inside the container.
        probe = r'''
spinup_port="$1"
spinup_timeout="$2"
spinup_seconds="$3"
if command -v python3 >/dev/null 2>&1; then
    python3 - "$spinup_port" "$spinup_timeout" <<'PY'
import socket
import sys
try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=float(sys.argv[2])):
        pass
except (OSError, ValueError):
    raise SystemExit(1)
PY
elif command -v node >/dev/null 2>&1; then
    node -e 'const net=require("net");const s=net.connect({host:"127.0.0.1",port:Number(process.argv[1])});let done=false;function finish(code){if(done)return;done=true;s.destroy();process.exit(code)}s.setTimeout(Number(process.argv[2])*1000,()=>finish(1));s.once("connect",()=>finish(0));s.once("error",()=>finish(1));' "$spinup_port" "$spinup_timeout"
elif command -v bash >/dev/null 2>&1; then
    bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$1"' spinup-bash-probe "$spinup_port"
elif command -v nc >/dev/null 2>&1; then
    nc -z -w "$spinup_seconds" 127.0.0.1 "$spinup_port"
else
    spinup_hex=$(printf '%04X' "$spinup_port")
    for spinup_table in /proc/net/tcp /proc/net/tcp6; do
        [ -r "$spinup_table" ] || continue
        while read -r spinup_slot spinup_local spinup_remote spinup_state spinup_rest; do
            case "$spinup_local" in
                *":$spinup_hex")
                    if [ "$spinup_state" = "0A" ]; then
                        exit 0
                    fi
                    ;;
            esac
        done < "$spinup_table"
    done
    exit 1
fi
'''
        args = [
            "exec", "--user", "root", self.name, "sh", "-c", probe,
            "spinup-port-probe", str(port), str(connect_timeout), str(seconds),
        ]
        try:
            result = self._run(args, max(2, min(8, int(timeout) + 2)))
            return result.returncode == 0
        except (SandboxError, OSError):
            return False

    def destroy(self) -> None:
        if self.destroyed:
            return
        self.destroyed = True
        for name in [self.name, *self.service_names, self.proxy]:
            try:
                self._run(["rm", "-f", name], 20)
            except SandboxError:
                pass
        try:
            self._run(["network", "rm", self.network], 20)
        except SandboxError:
            pass


def clean_runtime(runtime: str) -> None:
    if shutil.which(runtime) is None:
        return
    result = subprocess.run([runtime, "ps", "-aq", "--filter", "label=spinup=1"], text=True,
                            stdout=subprocess.PIPE, check=False)
    ids = result.stdout.split()
    if ids:
        subprocess.run([runtime, "rm", "-f", *ids], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    networks = subprocess.run([runtime, "network", "ls", "-q", "--filter", "label=spinup=1"], text=True,
                              stdout=subprocess.PIPE, check=False).stdout.split()
    if networks:
        subprocess.run([runtime, "network", "rm", *networks], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
