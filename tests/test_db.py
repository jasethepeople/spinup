import sqlite3
import tempfile
import unittest
from pathlib import Path

from spinup.db import Lane, LaneViolation, Memory
from spinup.models import FactDraft


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Memory(Path(self.tmp.name) / "test.db")
        self.repo = self.db.get_or_create_repo("https://github.com/acme/demo", "a" * 40, "python")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_repo_is_cached_by_url_and_commit(self):
        again = self.db.get_or_create_repo(self.repo["url"], self.repo["commit_sha"])
        self.assertEqual(self.repo["id"], again["id"])

    def test_lane_methods_reject_wrong_writer(self):
        with self.assertRaises(LaneViolation):
            self.db.add_fact(self.repo["id"], FactDraft("port", "8000"))
        with self.assertRaises(LaneViolation):
            self.db.add_attempt(self.repo["id"], 1, "start", "x", 0, "", "ok")

    def test_sqlite_authorizer_blocks_cross_lane_raw_writes(self):
        runner = self.db.lane_for(Lane.RUNNER)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                runner.conn.execute("INSERT INTO facts(repo_id,kind,value) VALUES(1,'port','8000')")
            runner.add_attempt(self.repo["id"], 1, "start", "python app.py", 0, "ok", "ok")
        finally:
            runner.close()

    def test_facts_and_attempts_are_append_only(self):
        analyst = self.db.lane_for(Lane.ANALYST)
        runner = self.db.lane_for(Lane.RUNNER)
        try:
            analyst.add_fact(self.repo["id"], FactDraft("port", "8000"))
            with self.assertRaises(sqlite3.DatabaseError):
                analyst.conn.execute("UPDATE facts SET value='9000'")
            with self.assertRaises(sqlite3.DatabaseError):
                analyst.conn.execute("DELETE FROM facts")
            runner.add_attempt(self.repo["id"], 1, "start", "x", 1, "z" * 5000, "fail")
            row = runner.conn.execute("SELECT log_tail FROM attempts").fetchone()
            self.assertEqual(4096, len(row[0]))
            with self.assertRaises(sqlite3.DatabaseError):
                runner.conn.execute("DELETE FROM attempts")
        finally:
            analyst.close()
            runner.close()

    def test_recipe_requires_orchestrator_and_is_immutable(self):
        self.db.save_recipe(self.repo["id"], "FROM x", None, "run", 8000, {"TOKEN": "fake"}, 1)
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.conn.execute("UPDATE recipes SET run_cmd='other'")
        self.assertEqual(1, self.db.recipe(self.repo["id"])["attempts_used"])

    def test_lesson_counters_accumulate(self):
        self.db.learn("python", "missing x", "install x", True)
        self.db.learn("python", "missing x", "install x", False)
        lesson = self.db.lessons("python")[0]
        self.assertEqual((1, 1), (lesson["worked"], lesson["failed"]))


if __name__ == "__main__":
    unittest.main()
