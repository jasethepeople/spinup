# SpinUp

SpinUp takes a public GitHub repository, performs read-only static analysis, builds a run plan, tries it in a fresh container, and saves a recipe only after the application process is alive **and** its port answers.

Python 3.11+, SQLite, Docker or Podman, and optional Ollama/Anthropic repair planning. The core package has no third-party Python dependencies.

## What is included

- `spinup run`, `facts`, `attempts`, `replay`, and `clean` commands
- Two clones per repository: one for the Runner and one for the Analyst
- SQLite lane enforcement plus append-only facts and attempt history
- Rule-based Python, Node, Go, Rust, and Compose detection
- Static port, environment, service, dependency, secret, and GPU analysis
- Up to eight attempts with logged setup, start, and verification steps
- Ollama repair by default, optional Anthropic, and deterministic fallback repairs
- Docker/Podman sandbox with an allowlisting Squid sidecar
- Mock sandbox for tests; it cannot be used to certify a real CLI repository run
- Dockerfile, Compose file, structured recipe, and run log export
- URL + commit caching and exact-commit replay
- Cross-repo lessons with worked/failed counters
- Basic app + PostgreSQL/Redis/MySQL/Mongo companion-service plans
- A 25-repository benchmark harness comparing Analyst and detector-only modes
- A manual real-container safety checker

## 1. Install on your Linux laptop

Install Git, Python 3.11 or newer, and Docker or Podman using your distribution's normal packages. Confirm the container engine works before installing SpinUp:

```bash
docker run hello-world
```

Extract the project, open a terminal in the extracted `spinup` folder, then run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cp config.example.toml config.toml
```

The supplied configuration uses Docker. For Podman, edit `config.toml` and change:

```toml
runtime = "podman"
```

Confirm the command is installed:

```bash
spinup --help
```

## 2. Optional LLM setup

Rule-based detection and planning work without an LLM. An LLM is used when rule-based planning is insufficient and after failures when a repair is needed.

Ollama is the default provider:

```bash
ollama pull qwen2.5-coder:7b
ollama serve
```

To use Anthropic instead, edit `config.toml`:

```toml
[llm]
provider = "anthropic"
model = "claude-3-5-haiku-latest"
enabled = true
```

Set the key in the host terminal, never in a repository or container environment:

```bash
export ANTHROPIC_API_KEY="your-key-here"
```

To disable LLM calls and use only deterministic repairs:

```toml
[llm]
enabled = false
```

## 3. Verify the real sandbox before using it

This step is mandatory before running untrusted repositories. It checks container creation, host-file isolation, secret exclusion, the network allowlist, the absence of host-published ports, port verification from inside the sandbox network, and timeout killing:

```bash
python scripts/docker_safety_check.py
```

Do not continue unless every check prints `PASS`. If it fails, save the exact output and fix the real backend first. Docker/Podman behavior is platform-specific and cannot be certified by mock tests.

## 4. Run a repository

```bash
spinup run https://github.com/user/repository
```

Machine-readable output:

```bash
spinup run https://github.com/user/repository --json
```

Detector-only comparison run:

```bash
spinup run https://github.com/user/repository --no-analyst
```

Results:

- **SUCCESS**: the process was alive and the port answered. The container was then removed and the recipe was saved.
- **FAILED**: execution or verification did not succeed within the attempt/repair policy.
- **UNSUPPORTED**: the project needs real credentials, GPU/hardware, an unknown entrypoint, or a repair SpinUp cannot safely infer.

SpinUp verifies an application; it does not leave the application running after the command finishes.

## 5. Inspect history and replay

Use the original GitHub URL:

```bash
spinup facts https://github.com/user/repository
spinup attempts https://github.com/user/repository
spinup replay https://github.com/user/repository
```

`replay` loads the saved plan, clones the exact verified commit even if the repository has moved forward, and verifies it again in a fresh container.

Remove clones and leftover SpinUp containers while retaining the database and exported recipes:

```bash
spinup clean
```

## 6. Recipes

A successful export creates:

```text
recipes/<owner>-<repo>-<commit12>/
  Dockerfile
  compose.yaml
  recipe.json
  run.log
```

SpinUp's own `replay` command uses `recipe.json`; it does not execute an untrusted repository's original Compose file.

To use Compose manually, copy the exported `Dockerfile` and `compose.yaml` into a checkout of the **same verified commit**, then run `docker compose up --build` there. The recipe directory deliberately does not contain a second source checkout. Manual Docker builds execute repository build code and do not provide every protection of SpinUp's runtime proxy, so use this only for code you trust.

## 7. Benchmark

After the safety checker passes, start small:

```bash
python scripts/benchmark.py --limit 3
```

Then run all 25 repositories in both modes:

```bash
python scripts/benchmark.py
```

The harness uses separate state directories for Analyst and detector-only runs, preventing cached success from contaminating the comparison. Results and per-repository timings are written to `benchmark-results/summary.json`.

The repository list intentionally includes applications, libraries, unsupported projects, and large projects. It measures the whole tool's ability to produce verified running apps; it is not a list of 25 expected successes. No benchmark success rate is claimed until this command actually finishes on a real container backend.

## 8. Tests

From the project folder:

```bash
python -m unittest discover -s tests -v
```

The suite executes the complete mock pipeline, failed-install repair, verification failure, caching, exact-commit replay, unsupported-secret handling, database lanes, append-only triggers, static-analysis non-execution, symlink exclusion, recipe round trips, and Docker command-policy construction.

See `VERIFICATION.md` for exactly what was and was not executed during delivery.

## Architecture

```text
GitHub URL
   |
   v
Git clone pair ---> Analyst (read-only) ---> facts
   |                                           |
   |                                           v
   |                                      SQLite (C)
   |                                           ^
   v                                           |
Runner (A) ---> plans/attempts/lessons ---------+
   |
   v
Fresh sandbox per attempt
   |
   +--> process alive AND port answers
                  |
                  v
          verified recipe export
```

The components communicate through `db.py`. SQLite's authorizer restricts each connection to its lane, and triggers reject updates/deletes to facts and attempts. Recipes are written only by the orchestrator after verification.

## Safety model and limits

- Only HTTPS `github.com/owner/repository` URLs are accepted. Git prompts, credential helpers, global Git config, and submodules are disabled.
- The Analyst reads files directly. It never imports or executes repository code.
- Containers receive a sanitized copy, not a host mount. `.git`, common credential files, private keys, and all symlinks are excluded.
- Environment values are generated fake stubs. Host API keys are never forwarded.
- App containers use an internal network. Outbound HTTP/HTTPS goes through a companion Squid proxy restricted to configured package/GitHub domains.
- App containers publish no ports to the host. Verification connects to `127.0.0.1:PORT` from inside the app container's own network namespace, using available in-container tools with a `/proc/net/tcp*` LISTEN-socket fallback.
- Capabilities are dropped; root filesystems are read-only; CPU, RAM, PID, tmpfs, and command-time limits are applied.
- Every container is removed after success or failure. `spinup clean` also removes labeled leftovers.
- Arbitrary original Compose files are **not** executed. Companion services are restricted to known images and configurations.
- No sandbox is magic. Kernel/container-engine vulnerabilities, malicious package content, allowed-domain abuse, and secrets embedded in ordinary source files remain possible. Start with trusted repositories even after all checks pass.

## Current functional limits

- Detection and planning target common single-application layouts, not every framework.
- Basic companion services cover PostgreSQL, Redis, MySQL, and MongoDB. Arbitrary multi-container Compose topologies need additional planning.
- Real paid APIs, GPU/CUDA, and exotic hardware are rejected rather than bypassed.
- GitHub access requires normal outbound HTTPS access from the host.
- Podman shares the Docker command wrapper but still needs its own manual safety run.
- A saved recipe proves a past verified run, not that every future environment will behave identically.

## Troubleshooting

**Docker not found / daemon unavailable**: confirm `docker info` and `docker run hello-world` work. For Podman, use `podman info` and update `runtime`.

**Registry access fails**: check the proxy image pull, DNS, and `network_allowlist` in `config.toml`. Add only required registry domains.

**Application starts but verification fails**: inspect `spinup attempts <url>`. The server may bind to `127.0.0.1`, use another port, or exit during startup.

**Constant timeouts**: increase `command_timeout_seconds` and `verify_timeout_seconds` in `config.toml`. Every container is still killed on command timeout.

**Weak repair results**: enable Ollama with a stronger local model or configure Anthropic. Never put real repository credentials into prompts or `.env` files.

**State reset**: recipes and history are intentionally retained by `clean`. To fully reset, stop SpinUp and remove `spinup.db`, `work/`, and generated recipe directories manually.
