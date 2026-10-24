PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS repos (
  id INTEGER PRIMARY KEY,
  url TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  stack TEXT,
  status TEXT DEFAULT 'new',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(url, commit_sha)
);

CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id),
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  source_file TEXT,
  confidence REAL DEFAULT 0.5 CHECK(confidence >= 0 AND confidence <= 1),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id),
  attempt_no INTEGER NOT NULL,
  step TEXT NOT NULL,
  command TEXT NOT NULL,
  exit_code INTEGER,
  log_tail TEXT,
  verdict TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recipes (
  repo_id INTEGER PRIMARY KEY REFERENCES repos(id),
  dockerfile TEXT NOT NULL,
  compose TEXT,
  run_cmd TEXT NOT NULL,
  port INTEGER,
  env_stubs TEXT,
  attempts_used INTEGER,
  verified_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY,
  stack TEXT NOT NULL,
  error_pattern TEXT NOT NULL,
  fix TEXT NOT NULL,
  worked INTEGER DEFAULT 0,
  failed INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(stack, error_pattern, fix)
);

CREATE TRIGGER IF NOT EXISTS facts_append_only_update
BEFORE UPDATE ON facts BEGIN SELECT RAISE(ABORT, 'facts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS facts_append_only_delete
BEFORE DELETE ON facts BEGIN SELECT RAISE(ABORT, 'facts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS attempts_append_only_update
BEFORE UPDATE ON attempts BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS attempts_append_only_delete
BEFORE DELETE ON attempts BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS recipes_no_update
BEFORE UPDATE ON recipes BEGIN SELECT RAISE(ABORT, 'recipes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS recipes_no_delete
BEFORE DELETE ON recipes BEGIN SELECT RAISE(ABORT, 'recipes are immutable'); END;

CREATE INDEX IF NOT EXISTS idx_facts_repo ON facts(repo_id, kind);
CREATE INDEX IF NOT EXISTS idx_attempts_repo ON attempts(repo_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_repos_url ON repos(url);
