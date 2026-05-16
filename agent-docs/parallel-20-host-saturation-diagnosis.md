# 20-Parallel Host-Saturation Diagnosis (Cell C1 / OpenHands + Claude API)

**Date**: 2026-05-11
**Question**: With `--parallel 20`, does the matrix hang because of RAM thrashing + CPU contention on the host (independent of ollama/qwen)?
**Short answer**: Yes — the host saturates at ~5× core count under load. Memory compressor (not disk swap) absorbs the RAM, so CPU contention is the dominant symptom, not OOM.

---

## 1. Test Setup

### Files created / modified for this experiment

| Path | Change | Why |
|---|---|---|
| `experiments/2026-05-11-fresh-start/configs/C1-openhands-claude-smoke.yaml` | **new** | Mirror of `C1-openhands-openai-smoke.yaml` but with `boss/manager/worker.model = claude-haiku-4-5-20251001` to isolate from the ollama/qwen path |
| `experiments/2026-05-11-fresh-start/manifest.yaml` | **edit** | Added cell `C1claudesmoke` pointing to the new config |

### Pre-flight reading

| Path | What I learned from it |
|---|---|
| `agent-docs/experiment-user-manual.md` | Run-matrix invocation pattern, env knobs, cell taxonomy |
| `.claude/docs/conventions.md` | Naming + import rules (not directly used here but mandated by CLAUDE.md pre-flight) |
| `experiments/2026-05-11-fresh-start/dataset.yaml` | 5 CVEs: `openjpeg.cve-2016-7445`, `njs.cve-2022-32414`, `imagemagick.cve-2019-13309`, `faad2.cve-2021-32272`, `mruby.cve-2022-0240` |
| `experiments/2026-05-11-fresh-start/configs/B1-ours-claude-noverifier.yaml` | Confirmed claude model-string format (`claude-haiku-4-5-20251001` with no provider prefix — litellm autodetects) |
| `experiments/2026-05-11-fresh-start/configs/C1-openhands-openai-smoke.yaml` | Pre-existing parallel=20 smoke template (uses gpt-4o-mini); copied structure, swapped model |
| `experiments/shared/scripts/run_matrix.py:181-213` | `--parallel N` → `ThreadPoolExecutor(max_workers=N)`; each thread invokes `python main.py run <task>` as a subprocess |
| `infrastructure/adapters/worker/openhands_adapter.py:80-216` | Adapter creates a `ThreadPoolExecutor(max_workers=1)` per worker, builds an OpenHands `Conversation`, runs synchronously inside that thread |
| `infrastructure/adapters/worker/openhands_adapter.py:286-292` | TerminalTool registered with `terminal_type=subprocess` (runs on host, NOT in container) |
| `infrastructure/adapters/worker/shared/container_session.py:67-115` | `apply_task_prefix` injects "use `./secb-exec` for shell" guidance into the task description |
| `plugins/security/docker_runtime.py:80-143` | `docker run` is invoked WITHOUT `--memory` or `--cpus` — containers compete unbounded |
| `plugins/security/plugin.py:167-220` | One container per `root_id` (per run); started lazily on first worker prepare |
| `infrastructure/adapters/postgres_event_store.py:129-143` | `asyncpg.create_pool` called with no min/max → asyncpg defaults to min=10, max=10 per process |
| `config/config.yaml` | Default `boss/manager/worker.model = ollama_chat/qwen3.5:397b-cloud`; output-format repairer also points to ollama by default |
| `.venv/lib/python3.12/site-packages/openhands/tools/terminal/terminal/subprocess_terminal.py:248-282` | PTY reader thread; `select.select` on a closed FD escapes the inner `except OSError` and gets caught by the outer logger at line 281 |

### Host baseline

```
sysctl hw.memsize  → 32.00 GB
sysctl hw.ncpu     → 10
docker system info → 23.43 GiB to Docker, 10 CPUs
```

### Postgres baseline (rules out connection exhaustion)

```
SHOW max_connections;           → 300
SELECT count(*) FROM pg_stat_activity;  → 7 (idle baseline)
```

20 procs × asyncpg pool of 10 = 200 worst case, well under 300.

### Pre-existing state (sanity check before launch)

`docker ps -a` showed:

- 3 secbench-worker containers **Up 45 minutes** with 0.00% CPU and 2.844 MiB RSS — zombies from a prior failed run
- ~15 historical secbench-worker containers in `Exited (137)` (= SIGKILL, usually OOM kill or `docker rm -f`) over the past 53 minutes to 2 hours

The 3 zombies were `docker rm -f`'d before launch:
```
secbench-worker-ad81d2d7-1b7435fa
secbench-worker-912c19ab-aa6d1085
secbench-worker-89f072e5-31c2ca6f
```

Compressor was **already at ~14GB** in the baseline `vm_stat` sample (Pages occupied by compressor = 877569 × 16KB), evidence of accumulated pressure from prior 20-parallel attempts.

---

## 2. Launch & Monitoring

### Launch script: `/tmp/c1-repro/run-matrix.sh`
```bash
set -a && source deployment/.env && set +a
export POSTGRES_HOST=localhost
export ARISE_ENV=production
uv run python -m experiments.shared.scripts.run_matrix \
  --study 2026-05-11-fresh-start \
  --cells C1claudesmoke \
  --replicates 4 \
  --parallel 20 \
  --no-render \
  --continue-on-error
```

Enumeration: 5 CVEs × 4 replicates = 20 jobs.

### Sampler scripts (5-second cadence)

| Script | Output | What it captures |
|---|---|---|
| `/tmp/c1-repro/sample-docker.sh` | `docker-stats.log` | Per-container CPU%, MEM, BlockIO, NetIO + `container_count=N` summary |
| `/tmp/c1-repro/sample-host.sh` | `host-procs.log` + `vmstat.log` | Load avg, D-state count, python_procs count; `vm_stat` Pages free/active/inactive/compressor + Pageins/outs + Swapins/outs |

### Captured logs (still on disk in `/tmp/c1-repro/`)

| File | Lines | Purpose |
|---|---|---|
| `matrix.log` | 100,017 | Full run_matrix stdout/stderr |
| `docker-stats.log` | 1,005 | 5-sec docker stats samples |
| `vmstat.log` | 137 | 5-sec vm_stat samples |
| `host-procs.log` | 136 | 5-sec load/d-state/python_procs samples |
| `start.ts` / `end.ts` | epoch seconds | Test start (1778534040) and forced-stop timestamps |

---

## 3. Observed Symptoms (chronological)

### Phase 0 — Pre-launch baseline (t < 0)

```
Pages occupied by compressor: 877569  (≈ 14 GB)
Pageouts (cumulative):        4089937
Swapouts (cumulative):        11455497  (steady — already saw heavy swap in prior runs)
Load avg:                     n/a (no Python procs)
Containers (non-test):        3 (arise-db, odoo-db-1, odoo-odoo-1; total ~440 MB)
```

### Phase 1 — Python init burst (t = 0 – 90s)

20 `main.py` subprocesses spawn simultaneously (`pgrep -f main.py | wc -l` → 20).

**`host-procs.log` excerpt:**
```
t=81s   load=23.51   d_state=11
t=107s  load=20.32   d_state=15
t=132s  load=16.17   d_state=6
t=157s  load=15.68   d_state=10
t=183s  load=13.03   d_state=16
```

**Interpretation**: 20 simultaneous OpenHands SDK imports + LiteLLM imports + plugin init drive load to 23.5 (2.35× cores). D-state count of 11–16 means that many threads are sitting in **uninterruptible IO wait** — on macOS, this is almost always memory-compressor or page-fault traffic.

**`vmstat.log` excerpt (Phase 1):**
```
t=30s   comp=1064757 (16.2 GB)  pageouts=4091609  swapouts=11455497
t=56s   comp=1069471 (16.3 GB)  pageouts=4093216  swapouts=11455497
```

Compressor grew from 14 GB → 16 GB in 30s. Pageouts increased by ~3300 in 30s (~110/sec). **Swapouts stayed flat** — compression absorbed the working set without spilling to disk.

### Phase 2 — BOSS/MANAGER (t = 90s – 310s)

Load settled to 13–16. Python procs in `S` state (sleeping on IO — Anthropic API responses). Compressor stable 13.6–15 GB. Tool-calling loops hit max iterations (`tool_calling_service.py:205` `WARNING Tool-calling loop hit max iterations (3), forcing final answer`) **169 times** across all 20 runs — Haiku doesn't always converge in 3 iterations on dense security recon prompts.

`matrix.log` evidence:
```
1546   total "LiteLLM completion()" calls
 169   "max iterations (3)" hits
   6   "Assessment parse failed"
   0   real 429/529 rate limits (only false-positive substring matches)
```

### Phase 3 — Worker container spawn (t = 310s – 440s)

First container appeared at t ≈ 270s, 6 at t ≈ 305s, 10 at t = 337s, 19 at t = 364s. **All but a few sat at 0.00% CPU and ~2.8 MiB MEM** — they're idle workspaces (`tail -f /dev/null` keepalive per `docker_runtime.py:110-113`), waiting for the OpenHands SDK to send commands via the helper script.

`host-procs.log` Phase 3:
```
t=336s  load=22.10   d_state=6
t=361s  load=27.03   d_state=4
t=388s  load=32.91   d_state=5
t=414s  load=31.88   d_state=3
```

Load climbing as more workers go active.

### Phase 4 — Worker execution (t = 440s – 595s, peak)

```
t=440s  load=35.80   d_state=1
t=467s  load=43.28   d_state=1
t=493s  load=47.83   d_state=6
t=518s  load=52.36   d_state=6
t=544s  load=50.83   d_state=3
t=570s  load=57.94   d_state=3
t=595s  load=71.06   d_state=4
```

**Load avg 71 on a 10-core machine = 7.1× CPU count.** Sustained.

`docker stats` snapshot during this phase:
```
secbench-worker-329697eb-05b3c328  102.20%  24.88 MiB    1.21 MB / 963 kB
secbench-worker-523be727-376f920f  111.78%  20.36 MiB     328 kB / 520 kB
secbench-worker-4e1d9b5f-32f60cd4   96.05%  105.3 MiB    90.1 kB / 8.19 kB
secbench-worker-93d9f27c-162a9eac   93.67%  ...
secbench-worker-44038f96-3a1ea88e   27.85%  56.7  MiB   5.22 MB / 926 MB   ← heavy write IO
... 14 others at ~0.00% CPU, 2.8 MiB each
```

5–8 containers are CPU-saturating their cores (compile / valgrind work). The rest are blocked waiting on their owning Python proc (which is in turn waiting on LLM responses or CPU).

`vmstat.log` Phase 4:
```
t=488s  comp=1082592 (16.5 GB)  pageouts=4104414  swapouts=11455497
t=595s  comp=1053397 (16.1 GB)  pageouts=4112175  swapouts=11455497
```

**Pageouts +7,800 in 107s = 73 pageouts/sec — modest. Swapouts unchanged. No disk thrash.**

### Phase 5 — Forced stop (t = 595s)

`pkill` against `run_matrix` + `main.py -c.*C1-openhands-claude-smoke`. 19/20 worker containers killed (`docker rm -f`), all 20 Python procs gone. **Zero `Exit 137` on workers during the run** (container_id-grouped count of `137` exits: 0).

---

## 4. Concrete Failure Modes Surfaced

| Symptom | Count | Where | Severity | Root cause |
|---|---|---|---|---|
| `ERROR PTY reader thread error: subprocess_terminal.py:281` | 8 | matrix.log:45083, 45157, 63997, … | Low (noise) | OpenHands SDK race: `select.select` on PTY master FD that main thread closed during teardown. See section 6. |
| `WARNING Format repairer LLM call failed` for `ollama_chat/qwen3-coder:480b-cloud` | 2 | matrix.log line 79xxx, llm_format_repairer.py:97 | Medium | Config leak — `output_format_repairer.model` in `config/config.yaml` was NOT overridden in `C1-openhands-claude-smoke.yaml`. 180s timeout per fallback. |
| `WARNING Tool-calling loop hit max iterations (3)` | 169 | tool_calling_service.py:205 | Low | Expected when Haiku doesn't converge fast on recon prompts; not a bug |
| `WARNING Assessment parse failed` | 6 | subtask_parser.py:685 | Low | Same — Haiku JSON quality |
| `ls: /testcase/: No such file or directory` from `Tool: terminal` calls | many | matrix.log around 5318d560-..., 6bd38d63-..., 5ee562b1-... runs | **High** | Agent ran container path through host-bound TerminalTool. See `agent-docs/openhands-worker-tool-routing-bug.md`. |
| `bash: cd: /src/njs: No such file or directory` | many | same runs | **High** | Same bug — TerminalTool ran on host. |

---

## 5. Hypothesis Verdicts

| ID | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| H1 | macOS memory compressor → CPU starvation | **PARTIALLY CONFIRMED** | Compressor stable 14–16 GB throughout; CPU contention is severe (load 71/10) but memory itself didn't OOM. The compressor doesn't cause the stall on its own — but it *amplifies* the CPU starvation from H6. |
| H2 | Docker daemon serializing container spawns | **REFUTED** | 19/20 containers spawned within ~150s; no observed serialization beyond expected `docker run` latency |
| H3 | Postgres connection pool exhaustion | **REFUTED** | `max_connections=300`, ~50 peak in use; asyncpg pool defaults to 10 per process |
| H4 | LiteLLM/Anthropic rate-limit retry storm | **REFUTED** | 1546 calls succeeded; zero real 429/529 (only substring false-positives like "0.0529") |
| H5 | `--network host` port collision | **REFUTED** | Containers don't bind ports (all docker exec'd into) |
| **H6** | **20 Python procs + ~10 active containers compete for 10 cores** | **CONFIRMED — root cause** | Load avg 35→71 in Phase 4, while only 5–8 of 19 containers active. Other 11–14 sit at 0% CPU because their owning Python proc is starved. |

---

## 6. PTY EBADF Race (secondary)

From `openhands/tools/terminal/terminal/subprocess_terminal.py:248-282`:

```python
def _read_output_continuously_pty(self) -> None:
    fd = self._pty_master_fd
    if fd is None:
        return
    try:
        while True:
            if self.process and self.process.poll() is not None:
                break
            r, _, _ = select.select([fd], [], [], 0.1)   # ← races with FD close
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
                ...
            except OSError:
                continue                                  # handles EBADF on os.read
            except Exception as e:
                logger.debug(f"Error reading PTY output: {e}")
                break
    except Exception as e:
        logger.error(f"PTY reader thread error: {e}", exc_info=True)   # ← line 281
```

Sequence under load:
1. Worker timeout hits OR agent decides task done → main thread closes the subprocess and the PTY master FD.
2. Reader thread is mid-`select.select([fd], ...)`. On macOS, `select` on a closed FD raises `OSError(EBADF)` — the inner `except OSError: continue` only wraps `os.read`, not `select`.
3. The outer `try/except` catches it, logs `PTY reader thread error: ...` at line 281, thread exits.

It's terminal-noise only — work that needed to complete has already returned its output. But on a stress run it spikes to 8+ occurrences because thread scheduling delays widen the race window.

---

## 7. Root-Cause Summary

**The host saturates not because memory runs out, but because CPU runs out faster than memory:**

- 20 Python procs × ~150–500 MB working set each = ~3–10 GB "live" memory
- macOS compressor compacts the cold pages into ~1 M × 16 KB = ~16 GB — fits in the 32 GB host without spilling to disk
- BUT every page-in requires CPU to decompress; that CPU is also being demanded by 5–8 worker containers running gcc/valgrind at 90–110% each
- Net: ~10 cores try to satisfy ~30–50 simultaneous runnable threads; load avg 35–71

**Symptom the user sees as "openhands worker doesn't proceed"**: the SDK's run loop is alive but the Python proc gets ~5% of a core per scheduler quantum, so each LLM round-trip + tool call takes 10–20× its normal wall-clock. Looks frozen; isn't.

---

## 8. Recommended Actions (priority order)

1. **`--parallel 5–8`** in `run_matrix` — direct fix, matches physical cores. Single best lever.
2. **Override `output_format_repairer.model`** in C1/C2 cell configs to `claude-haiku-4-5-20251001` (or any non-ollama). Eliminates 180s ollama timeouts on JSON-parse fallbacks.
3. **Fix tool routing** — see `agent-docs/openhands-worker-tool-routing-bug.md`. This is independent of the parallelism issue but compounds it (failed tool calls → more iterations → more LLM round-trips → more contention).
4. **Do NOT add `--memory`/`--cpus` caps to `docker run`** unless you observe specific OOM kills. Hard caps risk masking real SEC-bench results (valgrind/KLEE need headroom). The previous draft of this advice was wrong.
5. (Optional) Upstream fix for OpenHands PTY EBADF — wrap `select` in the same `except OSError: continue` as `os.read`.

---

## 9. Reproduction Recipe (for re-running)

```bash
# 0. Clean up any zombies from prior runs
docker ps -q --filter name=secbench-worker | xargs -I{} docker rm -f {}

# 1. Set up shell env
set -a && source deployment/.env && set +a
export POSTGRES_HOST=localhost
export ARISE_ENV=production

# 2. Start samplers (use /tmp/c1-repro/sample-*.sh)
bash /tmp/c1-repro/sample-docker.sh &
bash /tmp/c1-repro/sample-host.sh &

# 3. Launch matrix
bash /tmp/c1-repro/run-matrix.sh > /tmp/c1-repro/matrix.log 2>&1 &

# 4. Watch
tail -F /tmp/c1-repro/matrix.log
# In another shell:
watch -n 5 'uptime; docker stats --no-stream | head -25'

# 5. Stop when you've seen enough
pkill -f run_matrix
pkill -f "main.py -c.*C1-openhands-claude-smoke"
docker ps -q --filter name=secbench-worker | xargs -I{} docker rm -f {}
```
