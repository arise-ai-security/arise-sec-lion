# Experimental Design

> Status: DRAFT v0.1. **Code reality reviewed by 9 agents on 2026-05-12; see `findings/00-synthesis.md` for the gap.**
>
> **Critical review notes (revised after author Q&A, see `findings/00-synthesis.md` R1–R10):**
>
> - **D3 (Finding 04)**: "200+ CVE instances" not verifiable from local SEC-Bench repo. Source is the external HuggingFace dataset `SEC-bench/SEC-bench`. **Author confirms — cite the HF dataset, not the local repo.**
> - **D4 — CORRECTED (R3)**: `:patch` image has **no patch** but DOES have the PoC binary; `:poc` image has neither. The team correctly uses `:patch` for fix tasks (worker needs the PoC to verify reproduction before fixing). Caveat: PoC presence in `:patch` is a potential ground-truth-artifact-leak — see new threat TV16 in `03-threats-to-validity.md`.
> - **D5 — CORRECTED (R4)**: KLEE is wired in code (`plugins/security/security_tool.py:33-45`) with a lazy-install path (verified by `tests/test_security_tools_server.py:185-196`). The `config/config.yaml:153` comment "KLEE unavailable in Ubuntu Focal" is incorrect. Fix is one line: `tools: [valgrind]` → `tools: [valgrind, klee]`. No image rebuild needed.
> - **D7 — CLARIFIED (R5)**: Three distinct concepts, never conflate them in the paper:
>   1. **Judge** = our system's LLM-judge stage at the run boundary, toggled by `orchestration.skip_judge`. This is what B1↔B2 and C1↔C2 ablate.
>   2. **Verifier role worker** = our system's per-subtree terminal worker (each Builder / Exploiter / Fixer subtree ends with a worker whose role is "verify"). This is internal to the tree and is NOT the same as the Judge stage; not toggled by `skip_judge`.
>   3. **SecVerifier** = an external SEC-Bench-adjacent project at `~/PycharmProjects/SecVerifier`. Unrelated to our experiment; do not cite as the Judge.
> - **BUG-CR1 (Findings 08, 09)**: Smoke cells (`B1smoke`, `C1smoke`, `C1claudesmoke`) are in `manifest.yaml` alongside headline cells. `collect.py`/`render_report.py` do not filter them out. Headline aggregates will be polluted unless smoke cells are removed from `manifest.yaml` or a `headline_cells:` allowlist is added.
> - **BUG-CR2**: A1/A2 omit `worker.max_iterations_per_run`; B/C set it to 40. Add explicit `40` to A configs for provenance parity.
> - **R2 (Critical, post-author Q&A)**: A1/A2 do not crash — they **silently run as hierarchical** because `bootstrap/composition.py:115-133` never passes `mode=` to `ApplicationConfig` (which defaults to `"hierarchical"` at `bootstrap/application.py:96`). Every A-cell run to date has been a hierarchical run mis-labeled "A1" (confirmed by `runs/1c39be48-…/events.jsonl:1` showing `"role":"boss"`). Fix is in BUG-A1 in `findings/00-synthesis.md`.

## Independent variables (controlled)

Held constant across cells. Any drift here invalidates a between-cell comparison.

| Variable | Spec | Enforced in |
|---|---|---|
| Dataset | 200+ CVE instances from SEC-Bench `:patch`-tagged images | `experiments/2026-05-11-fresh-start/dataset.yaml` |
| Coding environment | SEC-Bench `:patch` Docker image (per-CVE) with `secb`, AI-generated PoC and repo, and benchmark binaries | sec-bench upstream (`~/PycharmProjects/SEC-Bench`) + repo plugin |
| User prompt | Single shared template + CVE instance info, identical across all cells | (TBD — pointer to template file) |
| Tool allowlist / denylist | Same shell tools and same MCP tools (e.g., Valgrind, KLEE, `secb`) across cells | Frozen in cell configs (TBD — verify) |
| Replicates per CVE | `replicates: 1` initially; scale up after smoke pass and pre-registration | `experiments/2026-05-11-fresh-start/manifest.yaml:9` |
| Random seeds | (TBD — must enforce per-cell determinism where the worker model supports it) | `experiments/shared/harness.py` |
| Wall-clock and cost budget per task | (TBD — verify same across cells) | configs |
| Worker SDK version pin | `openhands-sdk 1.20` (group C) | last commit `50277e8` |

## Manipulation (the IV under test)

System architecture, across six cells. Plus two judge-ablation variants.

| Cell | Group | Architecture | Worker model | Verifier (judge) |
|---|---|---|---|---|
| `A1` | A — Flat baseline | Claude Code CLI + native subagents | Claude (single model) | — |
| `A2` | A — Flat baseline | Claude Code CLI, no subagents | Claude (single model) | — |
| `B1` | B — Tree (ours) | boss → manager → worker | Claude SDK | OFF |
| `B2` | B — Tree (ours) | boss → manager → worker | Claude SDK | ON |
| `C1` | C — Tree (ours) | boss → manager → worker | OpenHands + Qwen | OFF |
| `C2` | C — Tree (ours) | boss → manager → worker | OpenHands + Qwen | ON |

Smoke variants exist (`B1smoke`, `C1smoke`, `C1claudesmoke`) for pipeline validation. They are NOT part of the headline matrix and must be excluded from paper results.

Config files: `experiments/2026-05-11-fresh-start/configs/{A1,A2,B1,B2,C1,C2}-*.yaml`.

### Orchestrator tool-calling boundary

BOSS and MANAGER nodes, plus PENDING assessment nodes, may use tool calling only for
orchestrator-side code reconnaissance / symbol-level analysis. In the `secbench`
domain the active toolset is `recon`, filtered to:

- `search_codebase`
- `read_file`
- `get_file_structure`
- `get_symbols_overview`
- `read_symbol`

These are read-only inspection tools used before assessment or decomposition. They are
not worker tools, do not execute builds/tests, and do not expose OpenHands tools such
as `file_editor`, `glob`, `grep`, `valgrind_run`, or `klee_run`. Worker tool access is
controlled separately by each cell's `worker.allowed_tools` / `worker.disallowed_tools`.

### Fairness across A vs B/C — second-level prompt injection

Claude Code CLI in `A1`/`A2` must receive **equivalent role prompts** to those used inside our tree's manager and worker nodes; otherwise the architecture difference is confounded with prompt content (see `03-threats-to-validity.md` → TV1).

Concretely: inject the second-level (Builder / Exploiter / Fixer) system prompts into Claude Code CLI's session, so the *task description content* is held constant and only the *coordination structure* varies.

**Verification required**: Confirm in `A1`/`A2` configs that this injection actually occurs and matches the prompts used by B and C. See `findings/code-review-A.md`.

## Dependent variables

### Quantitative (per task)

| Metric | Source | Per-cell consistency requirement |
|---|---|---|
| Tool-call count | Event stream (`experiments/shared/scripts/project_events.py`) | Same definition of "tool call" across A vs B/C — Claude CLI tool calls and our tree's tool calls must be normalized. |
| Total cost (USD) | LiteLLM / Anthropic billing telemetry | Per-cell cost-per-token rates may differ — report by-model breakdown. |
| Wall-clock time | Event stream timestamps | Per-host measured; control for `--parallel` worker count (cf. `agent-docs/parallel-20-host-saturation-diagnosis.md`). |
| Cheating attempts | TBD detector | Precise spec required (see `04-open-questions.md` → Q4). |
| Generated-code LoC | Artifact diff | Per-task diff applied to the SEC-Bench repo. |
| Ground-truth PoC reproduction | SEC-Bench verifier | Binary pass/fail. |
| Regression-test pass rate | SEC-Bench verifier | Binary pass/fail (existing project tests). |

### Qualitative (per task)

| Metric | Source | Notes |
|---|---|---|
| LLM-judge score on fix | Verifier model (TBD which) | Rubric pinned in `prompts/` before unblinding. |
| Conciseness / clarity | Judge or human | Rubric required. |
| CVE understanding score | Judge or human | Rubric required — e.g., "did the agent reference the actual root cause?" |

## Statistical plan

To be finalized in `01-hypothesis.md` § Statistical analysis plan. Skeleton:
- Paired-by-CVE comparison across cells (one CVE = one observation in each cell).
- Bootstrap CIs over CVE-level effects.
- Multiple-comparison correction across the six cells.
- Pre-register before any post-smoke run.

## Operational notes

- Concurrency / host saturation: see `agent-docs/parallel-20-host-saturation-diagnosis.md` and `agent-docs/concurrency-audit-{1..5}-*.md`. Saturation can confound wall-clock metric and tool-call latency — cap parallelism per cell or report by host load.
- DooD (Docker-out-of-Docker): cell containers may need volume mapping via `HOST_PROJECT_ROOT`; see `agent-docs/concurrency-audit-4-docker-dood.md`.
