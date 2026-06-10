# B4 cost-optimization — findings & changes (2026-06-09)

Goal: drive B4 (full 3-level tree, all roles) to be the cheapest arm, grounded entirely
in Postgres `events`. Roles unchanged; all numeric caps identical across cells (control);
every run must still produce an applying `model_patch.diff` (quality floor).

> Status: in progress. Sections 1–3 are final (grounded, won't change). Sections 4–6
> (controlled measurements, verdict) are filled after the cycle runs complete.

## 1. Billing audit — DB cost faithfully matches API billing ✅

For every model, the logged `cost_usd` equals an independent recompute from the event's
own token counts × web-verified provider rates, at **0.00% drift**:

| model | reported_usd (event) | recompute (tokens×price) | drift |
| --- | --- | --- | --- |
| gpt-5.3-codex (N worker) | $1.0255 | $1.0255 | 0.00% |
| gpt-5.4-mini (B worker) | $0.1178 | $0.1178 | 0.00% |
| claude-sonnet-4-6 (B mgr) | $0.4179 | $0.4179 | 0.00% |

No double-counting: each `WorkerCostRecorded.usage_metrics` array sums exactly to its
headline tokens. The OpenHands SDK and litellm both report `prompt_tokens` as **total
input inclusive of cache**, and `cost_usd = uncached·price_in + cache_read·0.1·price_in
(+ cache_write·1.25·price_in for Anthropic) + completion·price_out` — exactly the
providers' billing formula. **The DB is a faithful cost ledger for N1/N2 and B3/B4.**
(`study_sql check` drift stays 0.0 — keep it as the standing audit.)

## 2. Why N1/N2 OpenHands cache-hit is ~97% — real, not a bug

N1 faad2's single OpenHands session sent **3.40M** cumulative prompt tokens of which
**3.29M (96.8%) were cache_read**; uncached only 109K. Mechanism, proven from per-turn
`usage_metrics`: a long single agentic session **re-sends its growing conversation every
turn**; the provider caches the stable prefix; so cumulative cache_read dominates
cumulative input. It is correctly billed at the 0.1× cache rate.

**Key consequence (counter-intuitive):** a 97% hit rate does **not** mean the session is
cheap. That 3.29M cache_read still costs **$0.576 = 56% of the worker's $1.03** even at
0.1×. A long single session pays heavily in *cumulative* cache_read. B's modular workers
each have shorter sessions (89–92% hit), but there are 15 of them. So "high cache hit" is
not where B loses or N wins — absolute re-read volume is.

## 3. Where B4's cost actually is — runaway workers, not a structural floor

3-level B4 (openexr) worker tier = **$2.62 across 15 leaf workers**, but it is NOT uniform:

| worker (role) | prompt tok | turns | cost |
| --- | --- | --- | --- |
| Data-Flow-Analyst | 4.54M | 78 | $0.597 |
| PoC-Researcher | 2.26M | 64 | $0.403 |
| Root-Cause-Analyst | 2.42M | 63 | $0.368 |
| Forward-Instrumentator | — | 112 | (high) |
| …other 11 workers | 0.16–0.87M | 5–35 | $0.05–0.16 |

The cheapest worker is **$0.0495** (162K tok, ≤5 turns). **15 × cheapest = $0.74 — already
below N1's $1.03.** So the per-session fixed overhead is NOT what makes B4 expensive: a
handful of **runaway workers** (Data-Flow-Analyst, Forward-Instrumentator, PoC-Researcher,
Root-Cause-Analyst) dominate, each accumulating a huge context (~58K tokens/turn from
re-reading large source files) and re-sending it for 60–112 turns. Their cost is mostly
cumulative cache_read of that ballooning context.

**Lever tension (from the control rule):** the runaways are *context-size* driven, not
iteration-cap driven (they use 63–112 turns, under the 250 cap). An iteration cap can't
cleanly target them either — N2's flat+subagent agent itself uses **86 turns**, so any cap
low enough to clip B4's runaways (≤60) would break N2's quality floor. The reduction has to
come from shrinking the runaway workers' per-turn context (read targeted ranges / reuse
prior findings instead of re-reading whole files), which `roles/worker.j2:38–43` already
nudges but the runaways ignore.

## 4. Changes applied (all grounded, control-compliant)

- **Control harmonization** — `worker.max_iterations_per_run`, `worker.timeout`,
  `max_run_duration_seconds`, tool-call caps, `result_char_limit`, `token_budget` set to
  one identical value across N1/N2/B3/B4. (Fixed a real confound: N had 2500 iters/5400s
  vs B's 250/3600.) Cells now differ ONLY in topology + models.
- **Prompt trim** — `prompts/domains/secbench/cve.j2` CWE-tooling block 5505→2650 chars
  (kept every tool command + the CWE→tool mapping; dropped MITRE editorializing and the
  GOOD/BAD example). Shared by flat + hierarchical paths → identical for all cells, helps
  B4's 20 agents most in absolute terms.
- **Metric tooling** — `study_sql` gains `--latest N` (isolate one re-run cycle from prior
  runs of the same study_id) and `prov_norm_usd` (your normalization: Σ gpt cost +
  Σ anthropic cost ÷ 1.714, the claude÷codex input-price ratio).

## 5. Controlled measurement (cycle 1: control + trim), 2 instances

All 8 runs `exit_status=success`; B4 formed the full 20-agent/3-level tree both times;
**every run produced an applying `model_patch.diff`** (quality floor held). Billing recheck:
recompute == reported at **0.0 drift** for all three models. `study_sql cost --latest 1`:

| cell | agent-sessions | reported_usd | prov_norm_usd |
| --- | --- | --- | --- |
| **N2** | 2 | $1.85 | **$1.85** |
| **B3** | 10 | $3.71 | **$2.60** |
| **N1** | 2 | $2.90 | **$2.90** |
| **B4** | 40 | $10.12 | **$8.20** |

## 6. Run-to-run variance dominates modest levers

Same configs, different cycles: **N1 $1.90→$2.90 (+53%), B4 $7.89→$10.12 (+28%)** — pure
stochastic variance (N1's cache_read swung 5.8M→10.3M; the trajectory length varies per run).
The cve.j2 trim saves ~2% of the cached prefix; that is **invisible under ±30–50% run
variance**. Consequence: no modest config/prompt lever can be *measured* at 2 instances, and
the cost is governed by stochastic agent behavior (how far the runaway workers wander), not
by prompt size.

## 7. Verdict — B4 cannot be the cheapest arm (grounded, decisive)

**The hypothesis `N1,N2 > B3 > B4` (B4 cheapest) is architecturally inverted.** B4 runs **20
agent-sessions** (1 boss + 4 managers + 15 workers); the N cells run **1**. More sessions =
more cost — there is no tuning that makes 20 sessions cheaper than 1 while keeping all roles.

The decisive proof, fully grounded: take B4's **absolute best case** — every one of the 15
workers collapsed to the *observed cheapest worker* ($0.0495) **and** the manager tier
normalized by your ratio:

```
B4_best = 15·$0.0495·(2 instances)  +  manager_reported / 1.714
        = $1.49 (workers at floor)  +  $2.69 (mgr normalized)  =  $4.17
   vs cheapest cell  N2 prov_norm = $1.85     →  B4_best ($4.17) STILL > N2.
```

Even halving the manager tier on top of that leaves B4 ≈ $2.8 > N2 $1.85. **N2 is a single
primary OpenHands session — structurally the cheapest possible shape.** B4's manager tier is
pure cost the N cells never pay, layered on a worker tier that is 15 sessions where N is one.

Why the original premise ("B4 cheapest via maximized cache reuse") fails, from the events:
the workers already cache **89–97%**, yet that doesn't make them cheap — a long/again-large
session pays heavily in *cumulative* cache_read even at 0.1× (it is 56% of N1's cost, and the
bulk of the runaway workers' cost). Cache reuse was never the lever; **session count and
cumulative re-read volume are**, and B4 maximizes both.

## 8. What WOULD make B4 cheapest (none are config/prompt tuning)

1. **Collapse the 15 worker sessions into one shared-context session** (roles as in-session
   sub-steps, not 15 separate OpenHands conversations) — removes the 15× per-session
   cumulative-cache_read tax. This is an architecture change; it also blurs the "every role is
   a distinct agent" property the 3-level tree is meant to demonstrate.
2. **A different, defensible metric** — e.g. cost **per role-deliverable** (B4 emits ~16
   role artifacts vs N's few) or cost **per successful+correct patch** (if B4's modular
   validators raise success rate on harder CVEs). These can favor B4 but are a different claim
   than "cheapest per run"; they need a larger instance set to establish.
3. **Curb the runaway analysis workers' context growth** (Data-Flow-Analyst etc.) — legitimately
   reduces B4 cost and variance, but per §7 cannot get B4 below the single-session N cells.

## 9. Changes made (all kept — defensible improvements; none broke the quality floor)

| Change | File(s) | Effect |
| --- | --- | --- |
| **B4 forms the true 3-level tree, all roles** | `experiments/b4-…/configs/B4-…yaml` (`max_depth 2→3` +topology/run caps) | boss→{Builder,Exploiter,Fixer,Reporter}→15 leaf roles (PoC-Researcher, Forward-Instrumentator, Root-Cause-Analyst, Patch-Validator, Exploit-Validator, …). Verified 20-agent tree, all roles present. |
| **OpenHands wired into flat mode + `enable_subagents`** | `bootstrap/composition.py`, `config/settings.py`, `infrastructure/workers/openhands_worker.py` | N1/N2 run the same BEF pipeline + emit DB events; N2 gets native subagents (117 tests + pyright green). |
| **Control harmonization** | all 4 `configs/*.yaml` | `max_iterations_per_run`, `worker.timeout`, `max_run_duration`, tool-call caps, `result_char_limit`, `token_budget` identical across cells (fixed N=2500/B=250 confound). Cells differ only in topology + models. |
| **Prompt trim** | `prompts/domains/secbench/cve.j2` | CWE block 5505→2650 chars; every tool command kept. Shared → identical for all cells. |
| **Billing audit + cache analysis** | `study_sql check`; this doc §1–2 | DB cost == API billing at 0.0 drift, all models, no double-count. N's 97% hit explained + shown to be real and correctly billed. |
| **Metric tooling** | `experiments/shared/scripts/study_sql.py` | `--latest N` (isolate a cycle) + `prov_norm_usd` (your normalization: gpt cost + anthropic cost ÷ 1.714). |
| **Trajectory dump tool** | `experiments/shared/scripts/dump_trajectory.py` | events → text, tool calls as `TOOLCALL  <tool>  <content>`; `--nodes all|workers|<role>`. |

**Bottom line:** the DB is a faithful cost ledger (0.0 drift), N's high cache hit is real and
correctly billed, and B4 is now a correct full 3-level tree with every role. But B4 cannot be
the cheapest arm by any config/prompt/normalization lever — it performs 20 sessions of work
against the N cells' 1, and the grounded best-case still exceeds the cheapest N cell. Making
B4 literally cheapest requires an architecture change (shared-context workers) or a
different, separately-justified metric (per-deliverable / per-success), not parameter tuning.
