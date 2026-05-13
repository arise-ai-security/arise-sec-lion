# Arise Sec Lion

A **recursive, self-healing multi-agent orchestration platform** that decomposes complex security tasks into subtasks executed by specialized agents. The primary workload today is **SEC-bench**: take a CVE, build a sanitizer-instrumented reproducer, produce a patch, and prove the patch fixes the crash without regressing behaviour.

New to this repo? Read this file top-to-bottom once — it walks you from a fresh clone to a reviewed CVE run in about 15 minutes.

> **TL;DR.** The primary way to use this system is by hand: bring the stack up with `docker compose`, pick a fixture from `plugins/security/tests/fixtures/`, build the per-instance tools image, and run `docker exec arise-app python main.py run …`. Everything is visible at `http://localhost:8000`. Two **optional** Claude Code skills are also provided as a quick-shortcut layer — `/secbench-run` automates the full launch + monitor loop, and `/secbench-fixture` automates authoring a brand-new CVE fixture end-to-end.

## Contents

1. [Mental model](#1-mental-model)
2. [Prerequisites](#2-prerequisites)
3. [First-time setup](#3-first-time-setup)
4. [Run one CVE instance (by hand, with an optional skill shortcut)](#4-run-one-cve-instance-by-hand-with-an-optional-skill-shortcut)
5. [Where CVE fixtures live and how to pick one](#5-where-cve-fixtures-live-and-how-to-pick-one)
6. [Add more CVEs (`fetch_secbench.py` or `/secbench-fixture`)](#6-add-more-cves)
7. [Docker images on DockerHub](#7-docker-images-on-dockerhub)
8. [Where the artifacts land](#8-where-the-artifacts-land)
9. [Review results live — dashboard at `localhost:8000`](#9-review-results-live--dashboard-at-localhost8000)
10. [Review results through Claude Code automation (optional shortcut)](#10-review-results-through-claude-code-automation-optional-shortcut)
11. [Where security-related system prompts are written](#11-where-security-related-system-prompts-are-written)
12. [Troubleshooting cheat sheet](#12-troubleshooting-cheat-sheet)
13. [Use Ollama Cloud models (optional)](#13-use-ollama-cloud-models-optional)
14. [Project structure and docs index](#14-project-structure-and-docs-index)

---

## 1. Mental model

A single task is executed by a **tree of agents**. A root BOSS decomposes the task, each PENDING decides whether to split further (MANAGER) or do the work itself (WORKER). All state is event-sourced in Postgres; the React dashboard replays those events in real time.

```
BOSS (root)
  │ decomposes task into subtasks
  ▼
PENDING ──────────────────────────────┐
  │ evaluates complexity              │
  ├─── SIMPLE  ──►  WORKER            │
  │                   └─► executes    │
  └─── COMPLEX ──►  MANAGER           │
                      └─► spawns ─────┘ (recursive)
```

For SEC-bench the BOSS always follows a 4-phase process: **Builder → Exploiter → Fixer → Reporter**. Each phase becomes a MANAGER whose workers produce concrete artifacts (a build, a reproducer, a patch, a report) inside a per-instance Docker container.

| Layer | Pattern | Purpose |
|-------|---------|---------|
| **Domain** (`core/`) | Hexagonal (Ports & Adapters) | Pure business logic, no infrastructure imports |
| **Persistence** (`infrastructure/`) | Event sourcing | Append-only events, state via replay |
| **Query** (`query/`) | CQRS | Separate read models for efficient reads |

---

## 2. Prerequisites

- **Docker** and **Docker Compose v2+**
- **`uv`** (for fixture fetching and local scripts): `curl -LsSf https://astral.sh/uv/install.sh | sh`
- A **Claude Code** session in this repo root if you want to use the `/secbench-run` and `/secbench-fixture` shortcut skills. They're optional; see [`skills/README.md`](skills/README.md) for one-line install (symlink or copy into `.claude/commands/`). The skill source of truth lives under `skills/` because `.claude/` is gitignored
- A `deployment/.env` file with:
  - `POSTGRES_PASSWORD` (anything, local-only)
  - `OPENAI_API_KEY` (required for OpenAI-backed runs — LiteLLM default path)
  - `ANTHROPIC_API_KEY` (required if using the Claude Code worker)
  - `OLLAMA_API_KEY` + `OLLAMA_API_BASE=https://ollama.com` (optional — switch the
    stack to [Ollama Cloud](https://docs.ollama.com/cloud) by setting models to
    e.g. `ollama_chat/gpt-oss:120b` in `config/config.yaml`; see §13)
  - `HOST_PROJECT_ROOT=/home/<you>/arise-sec-lion` (needed for Docker-out-of-Docker volume mounts)

Copy the template: `cp deployment/.env.example deployment/.env` and fill in the values.

---

## 3. First-time setup

```
┌──────────────────────────────────────────────────────────────┐
│  git clone → cp deployment/.env.example .env → fill API keys │
│                                │                             │
│                                ▼                             │
│  docker compose --profile local up -d --build                │
│                                │                             │
│                                ▼                             │
│  wait for health (db, api = "healthy")                       │
│                                │                             │
│                                ▼                             │
│  open http://localhost:8000  (empty dashboard = success)     │
│                                │                             │
│                                ▼                             │
│  pick a fixture  →  /secbench-run                            │
└──────────────────────────────────────────────────────────────┘
```

Commands:

```bash
cd arise-sec-lion
cp deployment/.env.example deployment/.env   # fill in keys
docker compose --profile local up -d --build
docker compose --profile local ps            # wait until db + api are "healthy"
open http://localhost:8000                   # macOS; or paste into a browser
```

Three containers should appear:

| Container | Service | Purpose |
|-----------|---------|---------|
| `arise-app` | `app` | CLI entry point (sleeps until you `exec` into it) |
| `arise-db` | `db`  | PostgreSQL 16 (event store) |
| `arise-api` | `api` | REST + SSE + React dashboard on `http://localhost:8000` |

After editing Python code, rebuild only the app container: `docker compose --profile local up -d --build app`.

---

## 4. Run one CVE instance (by hand, with an optional skill shortcut)

**The primary path is manual: build the image, launch the CLI, watch the dashboard.** This is the recommended flow when you're learning the system — every step is visible and inspectable, and you can stop at any point to examine state. Once you're comfortable, `/secbench-run` is available as an **optional** Claude Code shortcut that bundles the same steps into a single invocation (§ Path B).

### The core loop

```
   ┌─ 1. pick a fixture                                               ┐
   │      plugins/security/tests/fixtures/<instance>.json             │
   ├─ 2. build the per-instance tools image                           │
   │      bash deployment/build-secbench-tools.sh <fixture>           │
   ├─ 3. launch the run inside the app container                      │
   │      docker exec arise-app python main.py run                    │
   │        --domain-context-file <fixture> --domain security "..."   │
   ├─ 4. watch it live                                                │
   │      http://localhost:8000  (tree + tabs)                        │
   └─ 5. inspect artifacts                                            │
          runs/<BOSS_ID>/testcase/                                  ┘
          ├─ security_report.md   ← human-readable summary
          ├─ model_patch.diff     ← the fix
          ├─ repro.sh             ← crash reproducer
          ├─ base_commit_hash     ← vulnerable commit
          └─ *.log, *.txt         ← per-worker tool output
```

### Path A — By hand (primary)

1. **Pick a fixture** from `plugins/security/tests/fixtures/*.json` (§5 lists known-good ones).

2. **Build the per-instance tools image.** This wraps the fixture's base image in an analysis-tools layer and tags it `secb-tools:<instance_id>-patch`. It's idempotent — skip this step if the image already exists.

   ```bash
   bash deployment/build-secbench-tools.sh \
     plugins/security/tests/fixtures/quickjs-ng.issue-1302.json
   ```

3. **Launch the run** inside `arise-app`:

   ```bash
   docker exec -it arise-app python main.py run \
     --domain-context-file plugins/security/tests/fixtures/quickjs-ng.issue-1302.json \
     --domain security \
     "Analyze quickjs-ng issue-1302: heap use-after-free in js_atomics_op, perform static analysis, build the project, reproduce the bug with the PoC, develop and verify a patch"
   ```

   Drop `-it` and redirect to a log if you want it to run unattended:
   ```bash
   docker exec arise-app python main.py run \
     --domain-context-file plugins/security/tests/fixtures/quickjs-ng.issue-1302.json \
     --domain security "..." > /tmp/run.log 2>&1 &
   ```

4. **Watch it live** at `http://localhost:8000` (see §9 for what each tab shows). For a terminal-only view:

   ```bash
   docker exec arise-app python main.py events --errors-only
   docker exec arise-app python main.py summary
   docker exec arise-app python main.py list --limit 5   # find the BOSS_ID
   ```

5. **Inspect artifacts** in `runs/<BOSS_ID>/testcase/` once the run finishes (§8 lists the deliverables).

### Path B — `/secbench-run` skill (optional shortcut)

If you're in a Claude Code session and want the same flow with one command, the `/secbench-run` skill bundles Path A steps 2–5 into an automated loop: it runs the image build, launches the CLI, polls the event DB every 2–5 minutes, sweeps a known-issue checklist, and prints a post-run verdict.

```
/secbench-run plugins/security/tests/fixtures/quickjs-ng.issue-1302.json
```

What the skill does (same work you'd do by hand in Path A, just chained together):

1. **Pre-flight**: checks containers are healthy, restarts them (unless `--no-restart`), verifies the fixture JSON.
2. **Image build**: runs `bash deployment/build-secbench-tools.sh <fixture>` (skipped if image already exists). Resolves the base image from `docker_image_override` if set, otherwise falls back to `hwiwonlee/secb.eval.x86_64.<project>.<cve-id>:patch`. There is **no automatic `songtli/...` fallback** — fixtures that should use the `songtli/` namespace must set `docker_image_override` explicitly (see §7).
3. **Launch**: execs `python main.py run …` inside `arise-app` in the background.
4. **Live monitor**: three DB queries every 2–5 min (agent count, event timeline, per-agent state summary).
5. **Issue diagnosis**: sweeps a checklist of known issues (missing image, scheduling bug, over-decomposition, role-name non-compliance, file-editor path errors, verification judge misfires, missing deliverables) and surfaces the fix.
6. **Post-run analysis**: timings, verification pass/fail, phase completion, artifact quality, comparison to previous runs.

Use this when you don't want to keep typing `docker exec …` and you want the issue checklist swept for you. You can always fall back to Path A to inspect any single step.

---

## 5. Where CVE fixtures live and how to pick one

**Authoritative source:** `plugins/security/tests/fixtures/*.json`. Every fixture already in-repo is runnable.

```bash
ls plugins/security/tests/fixtures/*.json
```

A fixture is a single JSON document. Minimum fields:

| Field | Example | Meaning |
|-------|---------|---------|
| `instance_id` | `quickjs-ng.issue-1302` | Unique ID; used to name the Docker image and output dir |
| `repo` | `quickjs-ng/quickjs` | GitHub `owner/repo` |
| `project_name` | `quickjs-ng` | Lowercase project identifier |
| `docker_image_override` | `songtli/secb.eval.x86_64.quickjs-ng.issue-1302:patch` | Base image to layer tools on top of (optional — see §7) |
| `lang`, `work_dir`, `sanitizer` | `c`, `/src/quickjs`, `address` | Build environment |
| `base_commit` | `537d004c…` | Vulnerable commit hash |
| `bug_description`, `sanitizer_report`, `bug_report` | text | Ground truth fed into the BOSS prompt |
| `build_sh`, `secb_sh`, `dockerfile` | text | Build + run scripts baked into the image |
| `exit_code` | `1` | Expected exit code when the PoC triggers |

Recent fixtures the team has validated end-to-end:
- `quickjs-ng.issue-1302.json` — JS heap UAF (post-cutoff, 2026)
- `ngtcp2.cve-2026-40170.json`
- `libretro-common.cve-2025-9809.json`
- `exiv2.cve-2018-19607.json`
- `jq.cve-2023-50246.json`
- `faad2.cve-2018-20198.json`
- `imagemagick.cve-2018-5247.json`
- `libarchive.cve-2019-11463.json`

Each entry in the list above has been run to completion on this setup — any of them are safe picks for a first run.

---

## 6. Add more CVEs

Two paths depending on whether the CVE is already in the upstream SEC-bench dataset — both end in a fixture the `/secbench-run` skill can pick up and automate:

```
              already in SEC-bench dataset?
                │                    │
            YES │                    │ NO
                ▼                    ▼
  fetch_secbench.py          author fixture by hand
    --instance-id X          (Dockerfile + build.sh + JSON —
  (scripted dataset pull)     see skills/secbench-fixture.md)
                │                    │
                │             optional shortcut:
                │               /secbench-fixture <gh-issue-url>
                │                    │
                ▼                    ▼
      plugins/security/tests/fixtures/X.json
                            │
                            ▼
                  run by hand (§4 Path A)
                            │
              optional: /secbench-run X.json (Path B)
```

### 6a. Pull from the SEC-bench HuggingFace dataset

The dataset is `SEC-bench/SEC-bench` with splits `cve`, `eval`, `oss`. The helper script saves one row per JSON file into the fixtures directory:

```bash
# install deps once
uv sync --frozen

# list what would be created (no writes)
uv run python plugins/security/tests/fixtures/fetch_secbench.py --dry-run

# fetch every instance from every split
uv run python plugins/security/tests/fixtures/fetch_secbench.py

# fetch a single instance by ID
uv run python plugins/security/tests/fixtures/fetch_secbench.py \
  --instance-id imagemagick.cve-2018-5247

# restrict to one split
uv run python plugins/security/tests/fixtures/fetch_secbench.py --split cve

# overwrite existing files
uv run python plugins/security/tests/fixtures/fetch_secbench.py --overwrite
```

### 6b. Create a brand-new fixture from a GitHub issue (optional skill shortcut)

For post-cutoff CVEs or bugs not yet in SEC-bench you can either write the fixture by hand (follow the patterns in `skills/secbench-fixture.md` — harness template, Dockerfile, patch-leak verification) or use the **optional** `/secbench-fixture` skill, which bundles the same workflow into a single command. Given a GitHub issue URL or CVE number, the skill:

1. Researches the vulnerability (issue body, PoC, sanitizer output, vulnerable commit, fix commit).
2. Generates a minimal C harness and PoC file that trigger the crash.
3. Writes a `build.sh` and `Dockerfile` that compile the vulnerable source with ASan while stripping git history to prevent patch leakage.
4. Builds, tests, and verifies the Docker image locally (confirms the crash reproduces and the fix commit is not visible).
5. Pushes the image to `docker.io/songtli/secb.eval.x86_64.<project>.<cve-id>:patch` and sets the DockerHub description.
6. Writes the final fixture JSON to `plugins/security/tests/fixtures/<instance_id>.json` and verifies it loads.

```
/secbench-fixture https://github.com/quickjs-ng/quickjs/issues/1302
```

Once it finishes, the fixture is immediately runnable via Path A of §4 (or the `/secbench-run` shortcut). See `skills/secbench-fixture.md` for the full procedure, which you can also follow manually step-by-step.

---

## 7. Docker images on DockerHub

Every fixture needs a base Docker image that contains the vulnerable source, ASan toolchain, and a `/usr/local/bin/compile` wrapper. The image is resolved by `CVEInstance.docker_image` (`plugins/security/cve_instance.py:41-44`) in exactly two steps:

1. **`docker_image_override`** field in the fixture JSON — used as-is if set. This is how the 11 team-authored fixtures point to `docker.io/songtli/...` images.
2. **Fallback default** — `hwiwonlee/secb.eval.x86_64.<project>.<cve-id>:patch`. Every fixture sourced from the upstream `SEC-bench/SEC-bench` HuggingFace dataset uses this path (300 instances as of 2026-05).

> **Note**: The two DockerHub namespaces have distinct provenance:
> - `hwiwonlee/...` — upstream SEC-bench paper authors' images. Matches every instance in the HF dataset.
> - `songtli/...` — this team's personal DockerHub. Used only for fixtures the team authored or rebuilt locally — all of these are **post-cutoff** (CVE-2025+ or `issue-*` entries not yet in the upstream dataset) and they explicitly set `docker_image_override`.

At run time, `deployment/build-secbench-tools.sh <fixture>` layers our analysis tools (cflow, cppcheck, gdb, ltrace, strace, valgrind, klee, etc.) on top of the resolved base image and tags the result `secb-tools:<instance_id>-patch`. That tagged image is what the workers actually exec into.

Manual build if the script ever fails:

```bash
docker pull songtli/secb.eval.x86_64.quickjs-ng.issue-1302:patch
docker build \
  -f deployment/secbench-tools.Dockerfile \
  --build-arg BASE_IMAGE=songtli/secb.eval.x86_64.quickjs-ng.issue-1302:patch \
  -t secb-tools:quickjs-ng.issue-1302-patch .
```

---

## 8. Where the artifacts land

Every run gets a UUID (the BOSS agent id) and a directory:

```
runs/<BOSS_ID>/
├── testcase/         ← all deliverables and tool outputs
├── src/              ← mirror of the vulnerable source tree
└── secb-exec         ← driver script used by workers
```

**Required deliverables** in `runs/<BOSS_ID>/testcase/`:

| File | What it is |
|------|-----------|
| `security_report.md` | Human-readable summary: CVE, root cause, fix, verification |
| `model_patch.diff` | The proposed patch (git-apply-able against `base_commit_hash`) |
| `repro.sh` | Standalone reproducer that triggers the original sanitizer error |
| `base_commit_hash` | The vulnerable commit the patch applies to |

**Per-worker tool output** you'll also see:

- ASan/UBSan logs (`asan_*`, `repro_asan.log`)
- Static analysis (`cppcheck_*`, `cflow_*`, `dataflow_*`)
- Dynamic analysis (`exploit_validator_gdb_*`, `*_ltrace.log`, `*_strace.log`, `*_valgrind_*`)
- Per-candidate PoC variants (`poc_candidate_*.js`) with stdout/stderr
- Build setup + compile reports (`build_setup_report.txt`, `build_compile_report.txt`)
- Root-cause + patch design notes (`root_cause_analysis.txt`, `patch_candidates_design_notes.md`)

A typical successful run has 50–120 files in `testcase/`. See `runs/04146448-7d88-4f04-9fa3-3e4b2180e679/testcase/` as a reference (the `quickjs-ng.issue-1302` success).

To find the latest run:

```bash
ls -td runs/*/ | head -1
# or
docker exec arise-app python main.py list --limit 5
```

---

## 9. Review results live — dashboard at `localhost:8000`

With the local profile up, `http://localhost:8000` serves the React dashboard (Vite-built, SSE-connected). Swagger is at `http://localhost:8000/api/docs`.

```
  VISUAL (primary)               CLI (primary)              ON DISK
  ────────────────               ─────────────              ───────
  Dashboard                      docker exec arise-app \    runs/<BOSS_ID>/
   @ localhost:8000                python main.py summary    testcase/
   ├─ Agent Tree                   python main.py events      ├─ security_report.md
   ├─ Summary                       --errors-only             ├─ model_patch.diff
   ├─ Events                       python main.py list        ├─ repro.sh
   ├─ Costs                        python main.py prompts     ├─ base_commit_hash
   ├─ Prompts                                                 └─ *.log, *.txt
   └─ Prompt Trace                OPTIONAL SHORTCUT
      (security tools +            /secbench-run skill
       supervisor insights)        (automates the DB queries +
                                    known-issue checklist so you
                                    don't have to run them by hand)
```

The dashboard + `main.py` commands are the primary review surface. The `/secbench-run` skill is an optional shortcut that sweeps the same data automatically.

What each panel gives you:

- **Agent Tree** — every BOSS / MANAGER / WORKER as a node. Click any node to focus the right-hand panels on it.
- **Summary tab** — the node's task, the supervisor context it received, and its latest produced events. This is where **"Insights"** (LLM-written observations for the supervisor) surface.
- **Events tab** — every domain event for the node (TaskAssigned, ComplexityEvaluated, SubtasksDefined, WorkCompleted, VerificationFailed, etc.), live-merged from SSE.
- **Costs tab** — per-run token + dollar totals, streamed via a dedicated SSE channel.
- **Prompts tab** — the exact prompts sent to the LLM for this node, including tool definitions.
- **Prompt Trace** (purple button top-right → `/prompt-trace/<rootId>`) — the full 4-tier composed prompt for every agent in the tree, with the **security tools** block and **supervisor insights** broken out. This is the best place to debug "why did the agent do that?"
- **Config modal** (gear icon) — read-only view of the effective YAML config.
- **Live indicator** (top-right) — green if the SSE stream is connected.

---

## 10. Review results through Claude Code automation (optional shortcut)

Reviewing by hand (dashboard + `python main.py events` / `summary`) is the primary workflow and is always enough. As an **optional** layer on top, the `/secbench-run` skill automates the review loop so you don't have to remember the queries or the checklist.

The skill's automated monitoring loop runs three queries every few minutes:

1. **Agent count** — should stay at 15–20 for SEC-bench. > 25 means over-decomposition.
2. **Event timeline** — last 60 high-signal events (completions, failures, retries, verifications).
3. **Per-agent state summary** — role, parent, latest status. Easy spot-check for stuck workers.

After each cycle it sweeps for known issues (see `skills/secbench-run.md` Phase 4 for the full checklist). When the run completes, it reads `security_report.md` and `model_patch.diff`, compares to previous runs, and prints a post-run verdict.

Use the dashboard as your default review surface. Reach for the skill when you want the issue checklist swept for you automatically, or when you're running several instances in a row and don't want to babysit each one.

---

## 11. Where security-related system prompts are written

Prompts use a strict 4-tier Jinja2 layering. **Security-specific text lives only in Tier 3.** Fixing a judge misfire or a wrong tool choice? Edit Tier 3 — never Tier 1 or Tier 2 (those are domain-agnostic).

```
Tier 0  prompts/system.j2                            (global persona)
Tier 1  prompts/roles/{boss,manager,pending,worker}.j2
Tier 2  prompts/operations/{assess,decomposition,execution}.j2
Tier 3  prompts/domains/secbench/                    ← SECURITY LIVES HERE
        ├─ boss.j2        4-phase decomposition + tool prescriptions
        ├─ manager.j2     manager-level security research guidance
        ├─ worker.j2      worker research mindset, tool iteration,
        │                 artifact naming conventions
        ├─ assess.j2      complexity assessment heuristics
        ├─ cve.j2         CVE context injection
        ├─ tools.j2       inventory of analysis tools + when to use them
        ├─ manager/       phase-specific manager decomposition
        │   ├─ builder.j2
        │   ├─ exploiter.j2
        │   └─ fixer.j2
        └─ worker/        phase-specific worker system prompts
            ├─ builder.j2
            ├─ exploiter.j2
            ├─ fixer.j2
            └─ reporter.j2
```

Rule of thumb:
- Wrong BOSS-level phase split → `boss.j2`
- Workers invoking the wrong analysis tool → `tools.j2` or the phase-specific `worker/*.j2`
- Manager over-decomposing → `assess.j2` or the phase-specific `manager/*.j2`
- Judge rejecting valid artifacts by filename → check the worker artifact-naming guidance, then the verification prompt (separate file: `core/application/services/orchestration/verification_pipeline.py` composes it)

---

## 12. Troubleshooting cheat sheet

| Symptom | Fix |
|---------|-----|
| Dashboard at `localhost:8000` is blank / `ECONNREFUSED` | `docker compose --profile local ps` — api container not healthy; `docker compose --profile local logs api` |
| Run fails at startup with "Missing SEC-bench image `secb-tools:<id>-patch`" | `bash deployment/build-secbench-tools.sh <fixture.json>` |
| `docker pull` of the base image fails | Check resolution order in §7; the fixture may reference a private image — set `docker_image_override` to `hwiwonlee/…` as a fallback |
| `fetch_secbench.py` fails importing `datasets` | Run inside `uv run`: `uv run python plugins/security/tests/fixtures/fetch_secbench.py …` |
| Run finishes but `runs/<id>/testcase/` is missing deliverables | The Reporter phase timed out; check the dashboard Events tab for that phase's VerificationFailed reasons |
| Agent count exploded past 25 | Over-decomposition — see `/secbench-run` Phase 4, edit `prompts/domains/secbench/assess.j2` |
| Need to re-run a stuck instance | `docker compose --profile local restart app` (or use `/secbench-run` which restarts automatically) |

Full diagnostic checklist: `skills/secbench-run.md` → Phase 4.

---

## 13. Use Ollama Cloud models (optional)

Ollama Cloud hosts large models (`gpt-oss:120b`, `gpt-oss:20b`, `deepseek-v3.1:671b`, …) behind a Bearer-auth `https://ollama.com` endpoint. LiteLLM's `ollama_chat/` provider speaks this protocol, and the OpenHands SDK uses LiteLLM under the hood — so switching any of BOSS / MANAGER / worker over is a config-only change.

1. **Create a key** at <https://ollama.com/settings/keys>.

2. **Add the env vars** to `deployment/.env`:

   ```env
   OLLAMA_API_KEY=<your-key>
   OLLAMA_API_BASE=https://ollama.com
   ```

   (LiteLLM reads both automatically; the `api_base` / `base_url` YAML fields below are optional overrides.)

3. **Point any or all roles at an Ollama model** in `config/config.yaml`:

   ```yaml
   boss:
     model: ollama_chat/gpt-oss:120b
     # api_base: https://ollama.com   # optional — env var is enough

   manager:
     model: ollama_chat/gpt-oss:120b

   worker:
     model: ollama_chat/gpt-oss:120b
     tool: openhands
     # base_url: https://ollama.com   # optional — env var is enough
   ```

   Use `ollama_chat/` (not `ollama/`) — it maps to the `/api/chat` endpoint Ollama Cloud exposes.

4. **Restart the app container** so new env vars and config are picked up:

   ```bash
   docker compose --profile local up -d --build app
   ```

That's the whole integration — no code-path differences from OpenAI runs. Cost tracking falls through LiteLLM's calculator; Ollama Cloud pricing is handled server-side, so `cost_usd` will report `0.0` for those calls.

---

## 14. Project structure and docs index

```
arise-sec-lion/
├── core/                    # Domain core — pure business logic, no infra imports
│   ├── domain/              # Aggregates, events, value objects
│   ├── ports/               # Abstract interfaces (Protocol)
│   ├── application/         # Use cases, orchestration
│   └── query/               # CQRS read models
├── infrastructure/          # Concrete adapters (Postgres, LiteLLM, workers)
├── plugins/security/        # SEC-bench security domain (fully removable)
│   └── tests/fixtures/      # ← CVE instance JSONs live here
├── bootstrap/               # Composition root, dependency wiring
├── presentation/            # CLI, formatters, renderers
├── query/                   # FastAPI + SSE + React SPA dashboard
├── config/                  # Pydantic Settings + YAML configuration
├── prompts/                 # Jinja2 prompt templates (4-tier)
│   └── domains/secbench/    # ← security-specific prompts
├── deployment/              # Docker Compose, Dockerfiles, build scripts
├── skills/                  # ← checked-in Claude Code slash commands
│                            #   (install into .claude/commands/ — see skills/README.md)
├── experiments/             # ← git-tracked comparative studies (see below)
│   ├── shared/              #   reusable harness, writers, validators
│   └── <study-id>/          #   one directory per study (manifest, configs, report)
└── runs/                    # ← run artifacts, one subdir per BOSS_ID (gitignored)
```

### Comparative experiments — `experiments/` vs. `runs/`

The repo uses a two-area split for comparative studies:

- `experiments/<study-id>/` — **small, git-tracked, derived products only.**
  Holds the study's `manifest.yaml`, pinned `configs/`, analysis `scripts/`,
  Jinja `templates/`, and rendered `reports/`. Every file under any
  `reports/` directory is emitted by a committed script and carries
  provenance metadata (YAML frontmatter for `.md`, `.generated.json`
  sidecar for binaries). The `validate-experiment-reports` pre-commit hook
  rejects any commit that violates this invariant.
- `runs/` — **large, gitignored, raw artifacts.** Flat pool keyed by
  `run_id` (= BOSS agent UUID). Each run is self-describing via
  `run_manifest.json`. Legacy runs migrated from the pre-2026-04 layout
  live under `runs/_legacy/`.

Primary harness: `python -m experiments.shared.harness run-ours …` /
`run-baseline …`. Full design: see
`docs/superpowers/specs/2026-04-22-experiments-layout-design.md`.

Deep dives in [`agent-docs/`](agent-docs/):

| Document | Description |
|----------|-------------|
| [architecture-concepts.md](agent-docs/architecture-concepts.md) | CQRS, Event Sourcing, Hexagonal, DDD |
| [architecture-separation.md](agent-docs/architecture-separation.md) | Generic vs security-domain boundary, plugin protocol |
| [domain-model.md](agent-docs/domain-model.md) | Agent lifecycle, domain events |
| [execution-flow.md](agent-docs/execution-flow.md) | How tasks flow through the system |
| [project-structure.md](agent-docs/project-structure.md) | Directory layout, layer responsibilities |
| [configuration.md](agent-docs/configuration.md) | Config files, secrets, environments |
| [api-reference.md](agent-docs/api-reference.md) | REST endpoints, SSE streaming |
| [development.md](agent-docs/development.md) | Testing, coding standards |
| [USER_MANUAL.md](USER_MANUAL.md) | CLI reference, plugin/recon configuration, dashboard setup |

## Tech stack

- **Runtime**: Python 3.12, `uv`, `asyncpg`
- **LLM routing**: LiteLLM (multi-provider), Claude Code, OpenHands, Google ADK workers
- **Storage**: PostgreSQL 16 (event store)
- **API**: FastAPI, SSE streaming
- **Frontend**: React 19 + Vite + TailwindCSS
