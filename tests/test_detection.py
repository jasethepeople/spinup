import json
import tempfile
import unittest
from pathlib import Path

from spinup.analyst import Analyst
from spinup.db import Memory
from spinup.detector import detect
from spinup.gitops import parse_github_url
from spinup.planner import RulePlanner, UnsupportedRepo


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_github_url_is_canonical_and_restricted(self):
        target = parse_github_url("https://github.com/Owner/repo.git")
        self.assertEqual("https://github.com/Owner/repo", target.url)
        for bad in ("http://github.com/a/b", "https://user:pass@github.com/a/b", "https://gitlab.com/a/b", "https://github.com/a/b/issues"):
            with self.assertRaises(ValueError):
                parse_github_url(bad)

    def test_node_detection_and_rule_plan(self):
        (self.root / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}, "dependencies": {"express": "^5"}}))
        (self.root / "package-lock.json").write_text("{}")
        detection = detect(self.root)
        self.assertEqual("node", detection.stack)
        memory = Memory(Path(self.tmp.name) / "db.sqlite")
        repo = memory.get_or_create_repo("https://github.com/a/b", "b" * 40)
        analyst = Analyst(memory)
        for fact in detection.facts:
            analyst.memory.add_fact(repo["id"], fact)
        plan = RulePlanner().plan(self.root, detection.stack, memory.facts(repo["id"]))
        self.assertEqual(["npm ci"], plan.setup)
        self.assertEqual("npm start", plan.start)
        self.assertEqual("0.0.0.0", plan.env["HOST"])
        analyst.close()
        memory.close()

    def test_analyst_never_executes_source(self):
        marker = self.root / "EXECUTED"
        (self.root / "requirements.txt").write_text("flask\n")
        (self.root / "app.py").write_text(f"from pathlib import Path\nPath(r'{marker}').touch()\nimport os\nos.environ.get('OPENAI_API_KEY')\n")
        memory = Memory(Path(self.tmp.name) / "db.sqlite")
        repo = memory.get_or_create_repo("https://github.com/a/b", "c" * 40)
        analyst = Analyst(memory)
        analyst.initial(self.root, repo["id"])
        facts = memory.facts(repo["id"])
        self.assertFalse(marker.exists())
        self.assertIn("OPENAI_API_KEY", {f["value"] for f in facts if f["kind"] == "needs_secret"})
        self.assertIn("real credentials", analyst.unsupported_reason(facts))
        analyst.close()
        memory.close()

    def test_compose_services_and_fake_database_url(self):
        (self.root / "requirements.txt").write_text("")
        (self.root / "app.py").write_text("")
        (self.root / "compose.yaml").write_text("services:\n  web:\n    build: .\n  postgres:\n    image: postgres:16\n")
        memory = Memory(Path(self.tmp.name) / "db.sqlite")
        repo = memory.get_or_create_repo("https://github.com/a/b", "d" * 40)
        analyst = Analyst(memory)
        detection = analyst.initial(self.root, repo["id"])
        plan = RulePlanner().plan(self.root, detection.stack, memory.facts(repo["id"]))
        self.assertEqual("python", plan.stack)
        self.assertEqual("postgres", plan.services[0]["name"])
        self.assertIn("postgresql://", plan.env["DATABASE_URL"])
        analyst.close()
        memory.close()

    def test_unknown_stack_is_unsupported(self):
        with self.assertRaises(UnsupportedRepo):
            RulePlanner().plan(self.root, "unknown", [])


if __name__ == "__main__":
    unittest.main()
