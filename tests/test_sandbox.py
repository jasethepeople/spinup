import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spinup.config import Settings
from spinup.sandbox import DockerSandbox, MockSandbox
from spinup.verifier import verify


class SandboxTests(unittest.TestCase):
    def test_mock_verifier_requires_process_and_port(self):
        box = MockSandbox(port_open=False)
        box.create(8000)
        box.copy_in(Path("."))
        box.exec("python app.py", 1, detach=True)
        self.assertFalse(verify(box, 8000, 0).ok)
        box.port_open = True
        self.assertTrue(verify(box, 8000, 1).ok)
        box.destroy()
        self.assertFalse(box.process_alive())

    def test_mock_timeout_kills_process(self):
        box = MockSandbox([("pip", 124, "slow", True)])
        box.create(8000)
        result = box.exec("pip install x", 1)
        self.assertTrue(result.timed_out)
        self.assertFalse(box.process_alive())

    def test_safe_copy_excludes_credentials_and_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            (source / ".git").mkdir()
            (source / "app.py").write_text("print('ok')")
            (source / ".env").write_text("REAL_SECRET=bad")
            (source / "private.pem").write_text("secret")
            host_secret = Path(tmp) / "host-secret.txt"
            host_secret.write_text("must not cross the boundary")
            (source / "linked-secret").symlink_to(host_secret)
            clean = Path(tmp) / "clean"
            DockerSandbox._safe_copy(source, clean)
            self.assertTrue((clean / "app.py").exists())
            self.assertFalse((clean / ".env").exists())
            self.assertFalse((clean / "private.pem").exists())
            self.assertFalse((clean / ".git").exists())
            self.assertFalse((clean / "linked-secret").exists())

    def test_copy_in_streams_sanitized_tar_to_exec_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "repro2.txt").write_text("hello")
            (source / ".env").write_text("REAL_SECRET=excluded")
            box = DockerSandbox(Settings.load(Path(tmp) / "state"), "python")
            observed = {}

            def consume_stream(args, stdin, timeout=None):
                observed["args"] = args
                observed["timeout"] = timeout
                names = []
                contents = {}
                with tarfile.open(fileobj=stdin, mode="r|") as archive:
                    for member in archive:
                        names.append(member.name)
                        if member.isfile():
                            contents[member.name] = archive.extractfile(member).read().decode()
                observed["names"] = names
                observed["contents"] = contents
                return subprocess.CompletedProcess(args, 0, stdout="")

            with patch.object(box, "_run_stdin", side_effect=consume_stream), patch.object(box, "_check") as check:
                box.copy_in(source)
            self.assertEqual(["exec", "-i", box.name, "tar", "-x", "-C", "/workspace"], observed["args"])
            self.assertEqual("hello", observed["contents"]["./repro2.txt"])
            self.assertNotIn("./.env", observed["names"])
            check.assert_called_once_with(["exec", "--user", "root", box.name, "chown", "-R", "1000:1000", "/workspace", "/home/app"])

    def test_stdin_run_path_uses_stream_not_buffered_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = DockerSandbox(Settings.load(tmp), "python")
            stream = io.BytesIO(b"not-buffered-by-spinup")
            completed = subprocess.CompletedProcess([], 0, stdout="")
            with patch("spinup.sandbox.subprocess.run", return_value=completed) as run:
                result = box._run_stdin(["exec", "-i", box.name, "tar", "-x", "-C", "/workspace"], stream, 7)
            self.assertEqual(0, result.returncode)
            self.assertIs(stream, run.call_args.kwargs["stdin"])
            self.assertNotIn("input", run.call_args.kwargs)
            self.assertEqual(7, run.call_args.kwargs["timeout"])

    def test_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load(tmp)
            args = DockerSandbox._limits(settings)
            self.assertIn("--read-only", args)
            self.assertIn("--cap-drop", args)
            self.assertIn("--pids-limit", args)
            self.assertFalse(any(arg in {"-v", "--volume", "--privileged"} for arg in args))

    def test_docker_create_commands_preserve_isolation_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load(tmp)
            box = DockerSandbox(settings, "python")
            calls = []
            def fake_check(args, timeout=None):
                calls.append(args)
                return ""
            with patch("spinup.sandbox.shutil.which", return_value="/usr/bin/docker"), patch.object(box, "_check", side_effect=fake_check):
                box.create(8000)
            app_create = next(args for args in calls if args[:2] == ["create", "--name"] and box.name in args)
            self.assertFalse({"-p", "--publish"}.intersection(app_create))
            self.assertFalse(any(arg.startswith("--publish=") for arg in app_create))
            self.assertIn("--read-only", app_create)
            self.assertIn("--cap-drop", app_create)
            self.assertFalse(any(arg in {"-v", "--volume", "--privileged", "--network=host"} for arg in app_create))
            network_create = next(args for args in calls if args[:2] == ["network", "create"])
            self.assertIn("--internal", network_create)
            self.assertEqual(8000, box.app_port)
            self.assertFalse(hasattr(box, "host_port"))
            self.assertFalse(any(args[0] == "port" for args in calls))
            proxy_create = next(args for args in calls if args[:2] == ["create", "--name"] and box.proxy in args)
            self.assertIn("ubuntu/squid:6.6-24.04_beta", proxy_create)
            self.assertNotIn("ubuntu/squid:latest", proxy_create)

    def test_port_answers_builds_ordered_internal_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = DockerSandbox(Settings.load(tmp), "python")
            completed = subprocess.CompletedProcess([], 0, stdout="")
            with patch("spinup.sandbox.subprocess.run", return_value=completed) as run:
                self.assertTrue(box.port_answers(8000, timeout=1.5))
            command = run.call_args.args[0]
            self.assertEqual(["docker", "exec", "--user", "root", box.name, "sh", "-c"], command[:7])
            probe = command[7]
            self.assertIn("socket.create_connection", probe)
            self.assertIn("net.connect", probe)
            self.assertIn("/dev/tcp/127.0.0.1", probe)
            self.assertIn("nc -z", probe)
            self.assertIn("/proc/net/tcp /proc/net/tcp6", probe)
            self.assertIn('"0A"', probe)
            positions = [probe.index(token) for token in ("command -v python3", "command -v node", "command -v bash", "command -v nc")]
            self.assertEqual(sorted(positions), positions)
            self.assertEqual("8000", command[-3])
            self.assertLessEqual(run.call_args.kwargs["timeout"], 8)

    def test_port_answers_returns_false_on_failure_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = DockerSandbox(Settings.load(tmp), "python")
            failed = subprocess.CompletedProcess([], 1, stdout="closed")
            with patch("spinup.sandbox.subprocess.run", return_value=failed):
                self.assertFalse(box.port_answers(8000))
            with patch("spinup.sandbox.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 4)):
                self.assertFalse(box.port_answers(8000))
            box.destroyed = True
            with patch("spinup.sandbox.subprocess.run") as run:
                self.assertFalse(box.port_answers(8000))
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
