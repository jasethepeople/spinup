import tempfile
import unittest
from pathlib import Path

from spinup.llm import LLMError, _extract_json
from spinup.models import Plan
from spinup.recipes import export_recipe, load_recipe, render_compose, render_dockerfile


class LLMRecipeTests(unittest.TestCase):
    def test_json_extraction_is_strict_about_object(self):
        self.assertEqual({"start": "x"}, _extract_json('```json\n{"start":"x"}\n```'))
        with self.assertRaises(LLMError):
            _extract_json("not json")
        with self.assertRaises(LLMError):
            _extract_json('[1,2]')

    def test_plan_validation(self):
        with self.assertRaises(ValueError):
            Plan.from_dict({"start": "x", "port": 70000})
        with self.assertRaises(ValueError):
            Plan.from_dict({"start": ""})

    def test_recipe_round_trip_and_fake_env(self):
        plan = Plan("python", ["python -m venv .venv"], ".venv/bin/python app.py", 8000, {"TOKEN": "spinup-fake-token", "PORT": "8000"})
        dockerfile = render_dockerfile(plan, "python:3.11-slim")
        compose = render_compose(plan)
        self.assertIn("USER 1000:1000", dockerfile)
        self.assertIn("spinup-fake-token", dockerfile)
        self.assertIn("internal: true", compose)
        self.assertIn("127.0.0.1:8000:8000", compose)
        self.assertNotIn("volume", compose)
        with tempfile.TemporaryDirectory() as tmp:
            path, _, _ = export_recipe(Path(tmp), "a-b", "a" * 40, plan, "python:3.11-slim", [])
            self.assertEqual(plan.to_dict(), load_recipe(path).to_dict())
            self.assertTrue((path / "run.log").exists())


if __name__ == "__main__":
    unittest.main()
