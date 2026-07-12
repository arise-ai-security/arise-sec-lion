# Manager prompt-cache: why it was 0% and how priming fixed it (0% → 88%)

**Status:** validated end-to-end and shipped (commit `5aff5c8`, env-gated `ARISE_PRIME_MANAGER_CACHE`, off by default).
**Scope:** cost-only. Does not change any success criterion. Manager tier ≈ 3.6% of run tokens.

## Symptom

In B4, the worker tier cached ~95% but the boss/manager (orchestration) tier cached ~0%.
Serializing managers (an earlier attempt) made them run one-at-a-time but cache stayed **0%**.

## Root cause (grounded in DB events + real prompts, run `459db8a8`)

Every link except the last checked out: managers serialized, `prompt_cache_key` set + plumbed
through `LLMQueryExecutor → ToolCallingService → adapter`, litellm forwards it
(`openai/chat/gpt_transformation.py:166`), model is clean `gpt-5.4`, and the two managers'
assessment prompts share a **32,523-char (~8,130-token) byte-identical prefix = 87.3%**
(diverging only at per-manager "Target paths"). `_extract_cache_tokens` reads
`prompt_tokens_details.cached_tokens`, so the 0 is real.

**The mechanism (proven by controlled litellm probes):**

> OpenAI serves a cached entry **only when that entry is a complete prefix** of the new request.
> It does not serve an arbitrary sub-prefix of a longer cached entry.

- Cold prefix, 87%-shared sibling → **0%** at 0s/4s/12s gaps, key and no-key alike.
- Identical prompt repeated → 96% (full match hits).
- **Prime a request with the shared prefix first → the sibling (which extends it) hits ~88%.**

Workers cache 95% because each turn = prior turn + appended text (the prior call *is* a literal
prefix). Sibling managers each make a **one-shot** assessment call and **diverge** mid-prompt, so
no manager's request is a prefix of another's → none warms the cache for its siblings. The same
one-shot structure is why boss cache was ~0.4%.

**Also proven:** tools segregate the cache. A tool-less prime is **not** hit by a tools-bearing
request (0%); the prime must carry the **same `tools` array** (89% when it does).

## Fix

Before the depth-1 managers dispatch (in parallel), issue **one** cheap request carrying their
**longest common (prompt-prefix, tool defs)** with the run's `prompt_cache_key`. Each manager's
real `query_with_tools` assessment then reads it back. No serialization needed — managers stay
parallel and all hit the primed node.

- `core/application/agent_orchestrator.py`: `prime_manager_cache()`, `_longest_common_prefix()`,
  and `_build_assessment_prompt_text()` (shared by `assess_task` and the prime so both emit
  byte-identical prompts); `prompt_cache_key = root_id` on manager calls.
- `core/application/execution_service.py`: one-shot trigger at manager dispatch
  (`_maybe_prime_manager_cache`); the old serialize-lock was removed.
- `infrastructure/adapters/litellm_adapter.py`: forwards `prompt_cache_key` for OpenAI models.

## Result (real B4 run, flag ON)

```
prime: "manager-cache primed: 4 managers"
MANAGER cache_read=35712 / prompt=40390 = 88.4%   (was 0%)
 managers ran 1.7s apart (parallel), each ~88%
```

## Caveats / fragility

- The primed prefix must stay a byte-exact prefix of the managers' real requests. It is, by
  construction (same `_build_assessment_prompt_text`, LCP is a true prefix), but it can silently
  degrade to 0% if the assessment prompt or tool defs drift between prime-time and assess-time
  (e.g. `shared_code_block` / agent-count changes). Failure mode is loss of the discount, never a
  wrong result. The `manager-cache primed: …` log line and a `cache_read=0` on managers surface drift.
- ROI: ~2% of total run cost at full hit rate, minus one prime request/run. Cost-only.
