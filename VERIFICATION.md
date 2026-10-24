# Delivery verification record

Date: 2026-10-24

## Confirmed by execution

- Python 3.12.12 and Git 2.39.5 were available in the build environment.
- All modules, scripts, and tests passed bytecode compilation.
- The package installed in editable mode with no third-party runtime dependencies.
- A standalone wheel built and installed successfully; its schema and all three prompt resources were present.
- The installed `spinup --help` entry point worked.
- The CLI returned the expected nonzero error for an unknown repository history lookup.
- 27 unit/integration tests passed with `python3 -m unittest discover -s tests -v`.
- The end-to-end mock pipeline produced and reloaded a recipe only after process + port verification.
- A simulated missing-module failure was repaired on attempt two and recorded as a worked lesson.
- A port-verification failure was not exported as success.
- Containers/test doubles were destroyed on tested success and failure paths.
- SQLite cross-lane raw writes and append-only violations were rejected by execution.
- Static analysis did not execute a source file containing a deliberate marker-writing side effect.
- A hostile symlink, `.env`, private key, and `.git` directory were excluded from the container copy staging area.
- Docker command construction was checked for internal networking, no `-p`/`--publish` arguments, read-only root, dropped capabilities, resource limits, and absence of mounts/privileged mode.
- Mocked subprocess tests checked the ordered in-container Python/Node/Bash/nc probe, its `/proc/net/tcp*` LISTEN fallback, short timeout, and False-on-failure behavior.
- The Squid default is pinned to `ubuntu/squid:6.6-24.04_beta`, not `latest`.
- Real local Git repositories verified `ls-remote`, two-clone creation, and exact old-commit replay after upstream HEAD moved.

## Attempted but blocked by environment

- A live `git ls-remote` against `https://github.com/octocat/Hello-World` timed out after 45 seconds. This establishes neither a Git implementation failure nor GitHub reachability on the user's laptop. Local real-Git checks passed.

## Not executed here

- Docker and Podman were not installed in the build environment. No real container run, network isolation test, image build, companion-service startup, or Docker/Podman replay is claimed.
- The new in-sandbox port probe and no-published-port behavior are NOT yet verified on a real Docker or Podman runtime. Only their command construction and failure behavior were unit-tested with mocks.
- `scripts/docker_safety_check.py` was compiled and its CLI help was checked, but its real-container checks were not run here.
- The 25-repository benchmark was not run. No real-repository success rate or Analyst improvement number is claimed.
- Ollama and Anthropic network calls were not made. JSON parsing and deterministic repair behavior were tested locally.

## Required acceptance sequence on the real host

1. Run `python -m unittest discover -s tests -v` and confirm 27 passing tests.
2. Confirm `docker run hello-world` or the Podman equivalent.
3. Run `python scripts/docker_safety_check.py`; require every check to pass.
4. Run `python scripts/benchmark.py --limit 3` and inspect all failures.
5. Run the full benchmark only after the small run and isolation checks are acceptable.

Real-container verification remains an explicit host-side gate, not an assumed result.
