# Design: forest-of-thoughts as an external SEC-bench runner (head-to-head)

- **Date:** 2026-06-22
- **Status:** Reviewed by codex 2026-06-22 → **sound-with-fixes**. Corrections folded in (see §12). Open decisions: model parity (headline), env-parity scope, dataset (see §11).
- **Authors:** garfield (+ Claude)
- **Repos in scope:**
  - `arise-sec-lion` (`/Users/garfield/PycharmProjects/arise-sec-lion`) — recursive multi-agent SEC-bench system + experiment harness. **All integration code lands here.**
  - `forest-of-thoughts` (`/Users/garfield/dev/forest-of-thoughts`) — the system under test. **Zero code changes** (driven via its existing CLI).
  - `SecVerifier-arise` (`/Users/garfield/PycharmProjects/SecVerifier-arise`) — the SEC-bench reference harness (OpenHands@0.34.0); source of the re-execution oracle.

---

## 0. Guiding principle — forest stays generic (INVARIANT)

**forest is a generic recursive coding-agent. The SEC-bench/arise head-to-head is ONE application of it, layered entirely on the arise side.** No arise / CVE / `secb` knowledge enters forest's core. The only forest changes permitted are **generic capabilities** useful to any task:

1. **Multi-provider LLM** (official SDKs, no LiteLLM) — §13.
2. **Generic `--mount HOST:CONTAINER[:ro]` passthrough** to the docker sandbox — forest never learns *what* is mounted; the arise runner composes the mounts.

Everything arise-specific — the runner, the `secbench-rerun` grader, the sanitized `/testcase`, the `secb` overlay + `repro.sh`/`patch.sh` seeding, artifact harvest, study config — lives in `arise-sec-lion/experiments/` and drives forest **only through its generic CLI**. If a proposed change would teach forest about CVEs, `secb`, or `/testcase`, it belongs on the arise side instead.

## 1. Goal

Run the **same SEC-bench "Build, Exploit, Fix" CVE task** that `arise-sec-lion` runs, but with **forest-of-thoughts** as the agent, and measure — under an **identical, provenance-proof grader** — whether forest's *emergent* tree decomposition solves CVEs as well as arise-sec-lion's fixed `BOSS→Builder→Exploiter→Fixer` pipeline and the OpenHands-linear baseline.

**Hypothesis (the WHY).** forest spawns *many* nodes, but every intermediate node of a given role shares a **byte-identical system + tools prompt prefix** (the 4-layer cache prefix — `forest-of-thoughts/.claude/docs/llm-plane.md` §4). Under prompt caching that whole prefix bills at **cache-read** rates across the entire tree, so a node's marginal cost collapses to its volatile tail. **Claim: forest's \$/solved-CVE is competitive with — or better than — arise's fixed pipeline and the OpenHands-linear baseline despite spawning more nodes, because the repeated prompt amortizes through cache.** ⇒ **cost (with explicit cache accounting) is the headline metric** (§8), and per-provider caching is first-class — the claim is untestable on GPT/Gemini/vLLM if their caching + cached-token reporting aren't wired (§13).

**Decisions already locked (with the user):**
1. **Rigorous head-to-head now** — matched dataset, same grader, matched budgets, N replicates, summary stats.
2. **Reuse arise's oracle** — grade forest's artifacts with the same SEC-bench oracle, not forest's own pass/fail.
3. **Forest runs inside arise's per-CVE docker image; arise grades** — no forest code changes; the intelligence lives in the goal/prompt + an arise-side runner.

---

## 2. The one hard constraint that shapes everything

Forest's entire acceptance model is *"a command exits 0 ⇒ pass."* Confirmed in code:
- Every check is `Check(cmd=("sh","-c",CMD))`; verifier pass/fail is purely `if exit_code != 0: fail` (`forest/infrastructure/sandbox/local_verifier.py:164`, `docker_verifier.py:112`, `forest/interface/cli.py:421`).
- **No "must-crash" / must-fail primitive.** There is no way to declare "this command MUST exit nonzero."
- **No two-state (before/after) verification.** The clean-room verifier checks the *committed post-change* state once (`local_verifier.py:54`); the join re-checks it once (`forest/core/application/join.py:18`). There is no "crash on the unpatched binary, no crash after patch."

SEC-bench's Exploit→Fix invariant is exactly *"crash before patch, no crash after patch."* So forest **cannot natively express the Exploit half** as a first-class check. This is **why the grader must be external** (decision 2/3): forest does the work; arise/SecVerifier's oracle — which re-executes `secb` itself — owns the crash-before/no-crash-after verdict.

(Forest *does* ship a `sec-bench` model strategy already: `forest/strategies/sec-bench/manifest.yaml` — a capability floor, not a CVE harness.)

---

## 3. Architecture: forest as a first-class external runner

arise's harness was built for exactly this. The runner registry docstring (`experiments/shared/runners/__init__.py:1-7`) reserves the path for *"external research baselines that can't be expressed as a config of our system."* Today there is **one** runner (`arise`); "OpenHands-linear" is **not** a separate runner — it is the `arise` runner with `worker.tool: openhands` config (`experiments/n1-openhands-linear/configs/N1-openhands-linear.yaml`). **Forest would be the first true external runner.**

### 3.1 Runner contract (`experiments/shared/runners/__init__.py:25-41`)
```python
@runtime_checkable
class Runner(Protocol):
    id: str
    label: str
    def run(self, *, study_id, cell, task, replicate, config, context_file) -> UUID: ...
    # optional async def run_async(...) — preferred by the dispatcher (run_matrix.py:301-307)
```
A runner is registered at import (`register(_Forest())`) and auto-discovered (`__init__.py:70`). The dispatcher selects it by the cell's `runner:` field (`run_matrix.py:244-247`).

### 3.2 What the forest runner must do (per `(cell, task, replicate)`)
1. **Parse** `context_file` → `CVEInstance` (`plugins/security/cve_instance.py:20-54`) → `docker_image` (`= hwiwonlee/secb.eval.x86_64.{project}.{cve}:patch`, `cve_instance.py:49-54`), `repo`, `base_commit`, `sanitizer`, `bug_*`, `sanitizer_report`, `build_sh`. **Unlike `_Arise`, the forest runner consumes `context_file`** (`_Arise` does `del context_file`, `runners/arise.py:31`).
2. **Host-clone** `repo@base_commit` as forest's `--workspace`.
3. **Run forest** (`forest run ... --platform docker --docker-image <cve image>`), see §4–§5.
4. **Harvest** forest's outputs from branch `forest/<root_id>` and **map** into `runs/<run_id>/testcase/...` per the grader's `_REQUIRED_FILES` contract (`experiments/shared/evaluation/criteria.py:63-84`; container→disk via `common.py:392-406`).
5. **Write** `runs/<run_id>/run_manifest.json` with `exit_status ∈ {completed,succeeded}` (else the matrix marks every job failed — `run_matrix.py:310-352`), then call `register_run(study_id, run_id, cell, task, replicate)` (`scripts/register_run.py:169-201`), and **return the `run_id: UUID`**.
6. **Label** any forest docker container `arise.session_pid=<os.getpid()>` so the matrix's stale-container sweep reaps it (`run_matrix.py:454-514`).

### 3.3 Study config to add (the "adopt our configs" work — arise-side)
- `experiments/forest-secbench/manifest.yaml` — cell `F1: {group: F, runner: forest, config: configs/F1-forest.yaml}`.
- `experiments/forest-secbench/dataset.yaml` — reuse an existing pinned CVE list + `source.paths` fixtures (so images are prebuilt and the arise baseline already exists).
- `experiments/forest-secbench/configs/F1-forest.yaml` — **a forest-runner-owned schema** (the runner parses it itself; it need not follow arise's `extends/overrides` overlay). Minimum keys:
  ```yaml
  model: <registry id matching arise's cell model>   # parity (§6)
  model_strategy: sec-bench
  max_seconds: 7800           # match ARISE_SUBPROCESS_TIMEOUT_SECONDS
  max_run_tokens: <matched>   # match arise --max-budget-per-task (price-converted)
  output:
    directory: ./runs         # keep the runs pool consistent
  ```

---

## 4. Per-CVE execution flow — resolving the `/ws` vs `/src` conflict

The image bakes the repo at `/src/<project>@base_commit` and seeds the PoC under `/testcase/`. Forest's docker model bind-mounts its *workspace worktree* to `/ws` (`forest/core/application/schedule.py:149`) and runs there; its clean-room verifier checks the committed `/ws` branch state.

⚠️ **Corrected (codex P0 #2):** as the CLI is wired, forest provisions **only** `{worktree: /ws}` (`schedule.py:151`) and passes no extra mounts. The per-CVE image's baked `/src` and `/testcase` exist in the image filesystem, but the **baked `/testcase` may still contain the gold `model_patch.diff`** — an anti-leak hole. Forest must mount a **sanitized** host `/testcase` *over* the baked one via the new `--mount` flag (§6/§12). `DockerSandbox.__init__` already accepts `mounts=` (`forest/infrastructure/sandbox/docker_runtime.py:69,119`); only the CLI/`build_deps` path needs the ~5-line passthrough.

**Resolution:** make `/ws` the source of truth for forest's edits.
- Forest's `--workspace` = a host clone of `repo@base_commit` (contents == `/src/<project>`). Bind-mounted to `/ws`.
- Forest **builds from `/ws`** with the image's sanitizer toolchain (guided by `build_sh`, which is *not* answer-key — §5), crafts/validates the PoC against that build, patches `/ws`, and self-verifies by rebuild+rerun. **Forest never calls `secb`** — that stays the grader's tool.
- The runner harvests `model_patch.diff` (`= git diff base..forest/<root_id>`) + the PoC input/invocation. Because `/ws` is a clone of the same `repo@base_commit`, the patch applies cleanly to the canonical `/src/<project>` at grade time.

**Input parity (mandatory) — corrected (codex):** forest must receive *exactly* what arise's exploiter sees after arise's seeding. Arise's `_remove_forbidden_testcase_artifacts` strips gold/candidate patches **and** `_seed_runtime_scripts` writes an **empty `repro.sh` skeleton** the exploiter must replace (`plugins/security/docker_runtime.py:30,70,176,179`). The earlier "keep the PoC input" wording was **wrong** — forest must **not** receive a pre-solved PoC/`repro.sh`, or the Exploit phase is unfairly easy. The runner builds this sanitized host `/testcase` and mounts it over the baked one. Forest then starts from *identical* materials to arise/OpenHands.

---

## 5. Forest goal/prompt + anti-leak

- A single Build→Exploit→Fix `--goal` + `--extra-context` carrying the CVE spec **minus the answer key**. Mirror `_PROMPT_FORBIDDEN_FIELDS = {patch, candidate_fixes, secb_sh}` (`cve_instance.py:15`, stripped via `to_template_context()` `cve_instance.py:98-110`).
- **Allowed context:** `build_sh`, `bug_description`, `bug_report`, `sanitizer`, `sanitizer_report`. **Forbidden:** `patch`, `candidate_fixes`, `secb_sh` (the gold `repro()`/`patch()` bodies).
- **Do not hand-decompose.** Feed the goal + deliverable contract + CVE context and let forest's planner build its own tree. Forest's *emergent* decomposition vs arise's fixed 4-phase pipeline vs OpenHands-linear is the experiment's central comparison.

---

## 6. Grading — OPEN DECISION (recommend A)

arise's full `criteria.py` verdict is **two-plane** (`criteria.py:838-987`): a file-plane (phase gates over `/testcase/*`) **AND** an event-plane. The event-plane breaks for an external runner: `runtime_seal_ok` requires a `RuntimeSurfaceSealed` event in Postgres, and the `execution_provenance`/`cve_reproduced` judges read arise's event stream (`evaluation/loading.py:96-100`). **A forest run does not populate arise's Postgres**, so the event-plane would unfairly fail forest.

- **A (chosen) — Independent re-execution oracle as the primary cross-system metric.** In a *fresh* per-CVE container, run `git reset --hard base && secb build && secb repro` (expect a sanitizer crash — `SANITIZER_ERROR_PATTERNS` `multi-agent.py:98-105`) then `secb patch && secb build && secb repro` (expect **no** crash), plus crash-class match and an over-broad-suppression check. Provenance-proof by construction (the grader runs `secb` itself; no system can fake it), needs no event stream, applies *identically* to forest / arise / OpenHands-linear. Report arise's native `criteria.py` two-plane verdict as a **secondary, arise-only** number.
  - ⚠️ **Corrected (codex P0 #1):** `complete_runtime` is **NOT artifact-only** — `complete_runtime(runtime: Runtime, …)` requires an OpenHands runtime and drives every command through `runtime.run_action` (`SecVerifier-arise/multi-agent.py:1015,1034,1061,1090`). So A means **building a standalone `secbench-rerun`** (fresh container, plain `docker exec`), **not** reusing `complete_runtime`. The earlier "~80-line" estimate is optimistic — the core must re-implement runtime/command/observation/env-capture/script-extraction/result handling (`multi-agent.py:1015-1522`). Feasible, but not a direct import.
- **B — Forge arise's events for forest** so `criteria.py` grades it natively. Brittle and circular (fabricating arise's anti-cheat evidence for a foreign system). **Rejected.**
- **C — Relax `criteria.py` seal/provenance for `external` cells** (a `cell_kind` flag). Couples to arise's grader; fallback if the two-plane verdict must be primary.

Rationale for A: it is the honest common denominator, faithful to "reuse arise's oracle" (it *is* SecVerifier's re-execution oracle), and the least code.

---

## 7. Parity controls (required for a valid head-to-head)

- **Model:** fix the same underlying LLM across systems. Forest resolves models via its registry + `--root-model`/`--model-strategy`; arise cells already pin e.g. `gpt-5.4` / `gpt-5.3-codex`. Forest's registry must contain that model. **Compare orchestration, not models.**
- **Budget:** match per-task token budget (`--max-run-tokens` ≈ arise `--max-budget-per-task`, price-converted) and wall-clock (`--max-seconds` ≈ `ARISE_SUBPROCESS_TIMEOUT_SECONDS=7800`; forest's default 600s/50-turn is far too small for C/C++ builds). Report actual cost spent (both systems track it).
- **Inputs:** identical `/testcase` seeding (§4).
- **Dataset:** reuse a pinned set — e.g. `experiments/shared/datasets/cve50-2026-06-09.lock.yaml` — so images are prebuilt and an arise baseline exists.
- **Replicates:** N=3 (matches the 3/3-determinism ethos; yields variance/CIs).

---

## 8. Metrics & reporting

**Headline — the hypothesis (§1): cost per solved CVE, decomposed to isolate the cache effect.**
- tokens split **cache-read / cache-write / uncached**, plus **cache-hit rate**, per system and per node-role.
- **\$ per solved CVE** and **\$ per run** (provider price × token class).
- a **counterfactual "cost without caching"** (all input tokens × full rate) → quantifies forest's cache amortization vs arise / OpenHands. Both systems measured on the **same provider** so caching is compared fairly (not forest-on-cheap-cache vs arise-on-expensive).
- **cost vs node-count** — does forest's per-node marginal cost flatten as the tree grows (the prediction)?
- ⚠️ **validity:** arise/OpenHands also cache; the claim is *comparative*, so the counterfactual + same-provider control are what make it meaningful. TTL matters — a tree spread over long wall-clock can let the 1h system / 5m subtree cache expire and re-pay cache-write; the read/write split measures this directly.

**Capability:** per-phase success rate (build / exploit / fix / overall) under the re-execution oracle (A), with CIs across replicates.

**Forest-specific:** tree shape (depth / branching / node count) vs the fixed 4-phase / linear pipelines; prefix reuse (distinct cached prefixes ÷ nodes).

Reuse `scripts/collect.py` → `enrollment.lock.yaml` and `scripts/write_report.py` → `matrix-summary.md`, extended with the cost/cache table. **Measurement prerequisite:** every forest adapter must surface `cache_read_tokens` / `cache_write_tokens` (§13) — without it the headline metric cannot be computed.

---

## 9. Risks & the 1-CVE smoke gate

| # | Risk | Mitigation |
|---|------|------------|
| 1 | `/ws`-vs-`/src` build friction — `build_sh` may hardcode `/src/<project>` | Adapt build invocation to `/ws`; flush out per-CVE in the smoke gate |
| 2 | Forest 600s/50-turn defaults too small for C/C++ builds | `--max-seconds 7800`, large `--max-run-tokens` (§7) |
| 3 | Forest's X1b test-audit critic may reject crash-oriented authored checks as "gamed" | Forest self-checks the *post-patch* (positive) invariant only; the crash-before judgment is external (oracle A) |
| 4 | Forest docker verify-phase is **untraced** (`forest/interface/cli.py:368-369`) | Accept (forest self-verification is not the official grade); oracle A is the source of truth |
| 5 | `complete_runtime` may be entangled with OpenHands | Extract a standalone `secbench-rerun.py` (§6) |
| 6 | Workspace boundary is detection-only; PoCs run real memory-corruption | Run on a sandboxed CI box, not a dev machine |

**Smoke gate (inside the rigorous plan, not a separate demo):** one fast-building CVE (e.g. `cjson.cve-2016-10749`) must pass end-to-end — host-clone → forest run → harvest → re-execution grade → `matrix-summary` — **before** launching the full matrix.

---

## 10. Work breakdown

**forest-side — GENERIC capabilities ONLY (own specs; useful beyond this experiment, per §0):**
- **G1. Multi-provider LLM** via official SDKs, no LiteLLM (§13) — *prerequisite*; unblocks model parity.
- **G2. Generic `forest run --mount HOST:CONTAINER[:ro]`** (repeatable) → `DockerSandbox(mounts=)`. No arise semantics; forest is agnostic to mount contents.

**arise-side — ALL arise/SEC-bench-specific code (in `arise-sec-lion/experiments/`):**
1. Standalone `secbench-rerun` grader (re-execution oracle, §6) — built fresh, **not** a `complete_runtime` import.
2. `experiments/shared/runners/forest.py` (§3.2) — composes the `forest run` cmd incl. the `--mount`s.
3. Forest goal/context builder + answer-key stripping (§5).
4. **Full env-parity replication** of arise's worker surface *via G2 `--mount`s*: sanitized `/testcase`, `secb` overlay, `repro.sh`/`patch.sh` seeding (§11.2). arise composes; forest just mounts.
5. Artifact-harvest/mapping forest branch → `/testcase` (§3.2.4) + independent manifest/enrollment/artifact/grader gates (§12).
6. Forest study config: `manifest.yaml` + `dataset.yaml` + `configs/F1-forest.yaml` (§3.3).
7. 1-CVE smoke gate (§9) — incl. a live-container filesystem leak check.
8. Full `cve50` matrix run + report (§8).

---

## 11. Open decisions (need a nod before implementation planning)

**Resolved with the user:** grading = **A**, now via a standalone `secbench-rerun` (§6/§12); `/testcase` leak closed by a small **`forest run --mount HOST:CONTAINER`** flag.

**Resolved 2026-06-22 (user):**
1. **Model parity → forest goes multi-provider.** Rather than pin one model, **make forest support the major AI providers via their official SDKs (explicitly NOT LiteLLM)** so it runs arise's exact baseline model (`gpt-5.3-codex` / `gpt-5.4`) and others on demand. This is a **prerequisite forest sub-project** with its own spec — see §13.
2. **Env parity = FULL runtime parity.** The forest runner reproduces arise's worker surface: mirror `/src`+`/testcase`+`/work` to host, overlay `/usr/local/bin/secb` read-only, seed `repro.sh`/`patch.sh` (`plugins/security/docker_runtime.py:152,173,263,284,304`). ⇒ `forest run --mount` must accept **multiple** `HOST:CONTAINER[:ro]` mounts.
3. **Dataset = `experiments/shared/datasets/cve50-2026-06-09.lock.yaml`** for the headline; 1-CVE smoke gate (`cjson.cve-2016-10749`) first.

---

## Appendix — key file:line anchors

**forest-of-thoughts**
- Run interface (argparse): `forest/interface/argparsing.py` (`_add_run_parser`); platform/docker `:128-139`.
- Check = exit-0 only: `forest/infrastructure/sandbox/local_verifier.py:164`, `docker_verifier.py:112`, `forest/interface/cli.py:421`.
- Clean-room verifier checks committed state: `local_verifier.py:50-69`.
- Join re-runs contract: `forest/core/application/join.py:18-26`.
- Docker branch in build_deps + untraced verifier: `forest/interface/cli.py:367-383` (note `:368-369`).
- Bind-mount worktree → `/ws`: `forest/core/application/schedule.py:149`.
- register_check / executor tools: `forest/core/application/llm_policies.py:375-410`; `forest/config.py:65-75`.
- sec-bench model strategy: `forest/strategies/sec-bench/manifest.yaml`.

**arise-sec-lion**
- Runner Protocol + registry: `experiments/shared/runners/__init__.py:25-77`.
- The only runner (template): `experiments/shared/runners/arise.py:26-75`.
- Dispatcher (runner-agnostic) + status-from-manifest: `experiments/shared/scripts/run_matrix.py:244-247,310-352,454-514`.
- Enrollment: `experiments/shared/scripts/register_run.py:169-201`.
- Grading (two-plane): `experiments/shared/evaluation/criteria.py:838-987`; required files `:63-84`; container→disk `experiments/shared/evaluation/common.py:392-406`; loader needs Postgres events `experiments/shared/evaluation/loading.py:96-100`.
- CVEInstance schema + forbidden fields + docker_image: `plugins/security/cve_instance.py:15,20-54,98-110`.
- `/testcase` seeding (forbidden artifacts): `plugins/security/docker_runtime.py:30-41`.
- Dataset lock: `experiments/shared/datasets/cve50-2026-06-09.lock.yaml`.

**SecVerifier-arise**
- Re-execution oracle (`complete_runtime`): `multi-agent.py:1015-1527`; sanitizer patterns `:98-105`.
- 21-CVE curated set: `config.toml:16-39`.

---

## 12. Codex review (2026-06-22) — verdict: **sound-with-fixes**

Independent codex pass verified the load-bearing claims against the code. Architecture holds; the following are folded into the sections above.

**Blocking fixes**
- **P0 grader** — `complete_runtime` is runtime-coupled, not artifact-only (§6). Build a standalone `secbench-rerun`.
- **P0 anti-leak** — forest mounts only `/ws`; the baked `/testcase` may carry the gold patch (§4). Mount a sanitized host `/testcase` via the new `--mount` flag.
- **P1 env parity** — arise mirrors `/src`+`/testcase`+`/work`, overlays `secb` read-only, seeds `repro.sh`/`patch.sh` (`plugins/security/docker_runtime.py:152,173,263,284,304`). The `--mount` fix closes the *leak*, not full runtime parity → decision §11.2.

**Manifest/status gates (codex's finding #5 enumeration — treat as separate gates, not one boolean):**
runner raises before returning a UUID · `run_manifest.json` missing · manifest malformed · returned UUID ≠ manifest path · `register_run` not called · enrollment exists but artifacts missing · `exit_status=completed` means *runner* completion **not** SEC-bench success · stale-container cleanup depends on the `arise.session_pid` label *and* the matrix reaching its cleanup phase. → The runner must gate **manifest, enrollment, artifacts, and grader result independently**.

**Residual validity threats**
- Baked-`/testcase` leak is broader than one file — image layers / other baked paths may hold answer material. **The smoke gate must inspect the live forest container filesystem**, not assume.
- PoC parity: forest must get arise's exact post-seed `/testcase` (empty `repro.sh`), never a pre-solved PoC (§4, corrected).
- `secbench-rerun` extraction is real work (runtime/command/observation/env/script/result handling), not a direct import.
- **Model mismatch (found post-review):** forest registry is Claude-only; arise baselines are GPT → decision §11.1.

**Codex recommendations:** grading A via standalone grader (not `complete_runtime`); keep C only as a secondary arise-native report; dataset `cve50-2026-06-09.lock.yaml` + smoke gate first. (Its `gpt-5.3-codex` model pin is **not viable** as-is — forest's registry lacks it; see §11.1.)

---

## 13. Prerequisite (GENERIC forest feature): multi-provider LLM via official SDKs

**This is a generic forest capability (§0), not an arise dependency** — the experiment merely needs it to match arise's baseline model.

**Starting point (from `.claude/docs/llm-plane.md`): forest is already provider-pluggable — harden + extend, not a rewrite.** `LLMPort` (`forest/core/application/ports.py`) is the seam, with `anthropic_adapter.py` (mature) and `openai_adapter.py` (**provisional**) behind it; the registry maps tiers per provider (`anthropic`/`openai`) and isolates per-family rules (`thinking_style`, `accepts_temperature`); provider selection is `_detect_llm` (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`). **Gaps:** OpenAI ids are placeholders (`gpt-5`/`-mini`/`-pro`, "confirm before live runs") — **not** arise's `gpt-5.3-codex`/`gpt-5.4`; the OpenAI adapter (tool-use, `reasoning_effort`, prefix caching) is unverified for live runs; no Google/other adapters exist.

Decision: **forest supports the major AI providers, each through its OFFICIAL SDK — explicitly NOT LiteLLM.** The provider-generic DTOs + 4-layer cache prefix + registry SSOT stay in core; each provider's mapping (tool schema, caching, reasoning/thinking, retryable errors) is isolated in its adapter + registry capability functions.

**Resolved provider set (native adapter per provider):** Anthropic (`anthropic`, exists) · OpenAI (`openai`, harden + real ids `gpt-5.3-codex`/`gpt-5.4`/`gpt-5.4-mini`) · Google Gemini (`google-genai`, new) · DeepSeek + vLLM (OpenAI-compatible → `openai` SDK + `base_url`; no distinct native client exists, so dedicated adapter *classes* over the `openai` SDK) · Ollama (`ollama` SDK, new). Core work generalizes two Anthropic-shaped abstractions (**caching → advisory hints**; **reasoning capability → data-driven per-model metadata in `models.json`**, not `claude-` prefix matching) and fixes a **pre-existing provider-threading bug** (`forest/interface/cli.py:358` builds `EscalatingConfigPolicy` with default `provider="anthropic"`, so leaf executors mis-resolve under non-Anthropic keys).

**This sub-project's spec lives in `forest-of-thoughts` (generic, per §0), not in this repo.** Blocking dependency of the head-to-head run.
