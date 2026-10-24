import shutil
import tempfile
import unittest
from pathlib import Path

from spinup.config import Settings
from spinup.orchestrator import Orchestrator
from spinup.sandbox import MockSandbox


URL = "https://github.com/acme/demo"
SHA = "a" * 40


class FakeCloner:
    def __init__(self, fixture):
        self.fixture = Path(fixture)
        self.clone_calls = 0

    def latest_sha(self, url):
        return SHA

    def _copy(self, destination):
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.fixture, destination)

    def clone_pair(self, url, base, sha):
        self.clone_calls += 2
        run, analyze = base / "run", base / "analyze"
        self._copy(run)
        self._copy(analyze)
        return run, analyze

    def clone_commit(self, url, destination, sha):
        self.clone_calls += 1
        self._copy(destination)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        (self.fixture / "requirements.txt").write_text("flask\n")
        (self.fixture / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
        self.settings = Settings.load(self.root)
        self.settings.llm["enabled"] = False
        self.settings.verify_timeout_seconds = 0
        self.cloner = FakeCloner(self.fixture)
        self.boxes = []

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self, scripts=None):
        scripts = list(scripts or [])
        def create(plan):
            responses = scripts.pop(0) if scripts else []
            box = MockSandbox(responses)
            self.boxes.append(box)
            return box
        return create

    def app(self, scripts=None):
        return Orchestrator(self.settings, self.cloner, self.factory(scripts), allow_mock=True)

    def test_end_to_end_success_export_and_cache(self):
        with self.app() as app:
            result = app.run(URL)
            self.assertEqual("success", result.status)
            self.assertEqual(1, result.attempts)
            recipe = Path(result.recipe_path)
            for name in ("Dockerfile", "compose.yaml", "recipe.json", "run.log"):
                self.assertTrue((recipe / name).is_file())
            self.assertTrue(all(box.destroyed for box in self.boxes))
            cached = app.run(URL)
            self.assertTrue(cached.cached)
            self.assertEqual(2, self.cloner.clone_calls)
            self.assertEqual("success", app.memory.get_repo(result.repo_id)["status"])

    def test_failed_install_is_repaired_and_lesson_saved(self):
        scripts = [[("pip install -r", 1, "ModuleNotFoundError: No module named 'missing'", False)], []]
        with self.app(scripts) as app:
            result = app.run(URL)
            self.assertEqual("success", result.status)
            self.assertEqual(2, result.attempts)
            self.assertEqual(1, app.memory.lessons("python")[0]["worked"])
            self.assertTrue(any("pip install missing" in a["command"] for a in app.memory.attempts(result.repo_id)))
            self.assertTrue(all(box.destroyed for box in self.boxes))

    def test_verification_failure_is_not_reported_as_success(self):
        self.settings.max_attempts = 1
        def broken_factory(plan):
            box = MockSandbox(port_open=False)
            self.boxes.append(box)
            return box
        with Orchestrator(self.settings, self.cloner, broken_factory, allow_mock=True) as app:
            result = app.run(URL)
            self.assertEqual("failed", result.status)
            self.assertIsNone(result.recipe_path)
            self.assertTrue(self.boxes[0].destroyed)

    def test_required_real_secret_is_unsupported_without_container(self):
        (self.fixture / ".env.example").write_text("OPENAI_API_KEY=required\n")
        with self.app() as app:
            result = app.run(URL)
            self.assertEqual("unsupported", result.status)
            self.assertEqual(0, result.attempts)
            self.assertEqual([], self.boxes)

    def test_replay_uses_saved_plan_and_records_new_history(self):
        with self.app() as app:
            first = app.run(URL)
            replay = app.replay(URL)
            self.assertEqual("success", replay.status)
            self.assertEqual(3, self.cloner.clone_calls)
            self.assertGreater(len(app.memory.attempts(first.repo_id)), 4)

    def test_detector_only_mode_skips_static_env_scan(self):
        (self.fixture / "app.py").write_text("import os\nos.getenv('OPTIONAL_TOKEN')\n")
        with self.app() as app:
            result = app.run(URL, use_analyst=False)
            kinds = {f["kind"] for f in app.memory.facts(result.repo_id)}
            self.assertNotIn("env_var", kinds)
            self.assertEqual("success", result.status)


if __name__ == "__main__":
    unittest.main()
