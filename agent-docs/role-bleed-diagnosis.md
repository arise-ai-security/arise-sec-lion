# Role-Bleed Diagnosis — Exploiter Worker Tier Erases the Role Layer

**Date:** 2026-06-15
**Author:** analysis session (Claude), independently cross-checked by Codex-authored SQL + source code.
**Status:** VALIDATED against the event store (Postgres `arise_events`, 173 runs / 964 agents, window 2026-06-13 → 2026-06-16).

> Purpose: a fresh session can re-run every query below against the event store and reproduce the conclusion. Numbers are derived from event payloads plus a few table columns (`event_type`, `aggregate_id`, `sequence_number`, `occurred_at`), not inference.

---

## BLUF / Verdict

The system's novelty is the **role layer** (`assess.j2` slices each phase into narrow roles with distinct `produces`/`depends_on`). The manager **partially** creates that layer — required roles are under-spawned at the **run** level: of 173 runs only **57** spawn an Exploiter manager, and only **15 of those 57** spawn all six required Exploiter leaf roles. So the owner role often never exists in the tree. What it does create is **erased at the worker tier** because the worker prompt has no role-gated body: `prompts/domains/secbench/worker/exploiter.j2` includes the full-phase `phases/exploit.j2` runbook for *every* Exploiter role. Consequence, measured in events:

- Roles do **not** own their deliverables. The owner role writes its own deliverable in a minority of runs (`repro.sh` 49%, `exploit_validation` 21%); the rest are produced **exclusively by a non-owner**. Robust to authorship channel: folding in `MCPToolAction` shell write-redirects (not just the file-editor) keeps owner < non-owner among Exploiter leaves (`repro.sh` 22 vs 37; `exploit_validation` 10 vs 49).
- The pathology is **owner abdication** (a single *wrong* role produces the file), NOT "many cooks pile onto one file" (that happens in only 7–20% of runs).
- The redundancy is **cross-role / cross-aggregate**, so it is **retry-immune** (retry is within-aggregate). 60/61 workers edited `repro.sh` exactly once.
- The structured handoff channel (`<decision>`→`DecisionRecorded` and `<output>`→`ArtifactStored`) is under-used: combined publishers top out at **37.5%** of workers (Regression-Tester); the deliverable-owner roles publish least (Repro-Creator 6.5%, Exploit-Validator 2.3%). Handoff is de-facto filesystem-only.

The original claim and prior analysis were correct in direction. The only correction makes the claim **stronger**: it is abdication, not pile-on.

---

## Ownership contract (source of truth)

From `plugins/security/roles.py` + `plugins/security/deliverables.py`:

| Deliverable path | Owning role |
|---|---|
| `/testcase/repro.sh` | **Repro-Creator** |
| `/testcase/poc_path.txt` | **Repro-Creator** |
| `/testcase/exploit_validation_results.txt` | **Exploit-Validator** |

Research roles (PoC-Researcher, Data-Flow-Analyst, PoC-Tester, Forward-Instrumentator) own **no** `/testcase` deliverable; they are supposed to produce insight files + publish `<context-update>` and hand off.

---

## Data model used for attribution

- One agent = one `aggregate_id`. Its **role** = the `[Bracket]` prefix of its `TaskAssigned.payload->>'task_description'` (regex `^\[([^]]+)\]`).
- File production = `SourceFileEdited{ payload->>'path', payload->>'edited_by' }`. `edited_by` is the producing agent's uuid. Join `edited_by → TaskAssigned bracket` = the producing role.
  - Verified in `infrastructure/adapters/worker/openhands_adapter.py:70`: `SourceFileEdited` fires only for the OpenHands **file-editor** commands `{str_replace, create, insert, undo_edit}` and **excludes `view`** → it captures editor-producers, not readers.
  - **Caveat (do not overstate):** `SourceFileEdited` does NOT capture **shell/MCP writes** (`echo>`, `cat <<EOF >`, `tee`, redirects), which run via `MCPToolAction`. To measure *final* filesystem authorship, also count shell write-redirects in `ThoughtCaptured` (see Query 7). Empirically these are rare for the contract files (`repro.sh`: 60 write-redirects across all runs) and do not flip any conclusion, but the raw percentages are "editor + shell" authorship, not a guarantee of the last writer.
- Handoff channel = BOTH `<decision>`→`DecisionRecorded{ decided_by, decision_key }` AND `<output>`→`ArtifactStored{ stored_by, key }`. Verified in `core/domain/services/context_update_parser.py:32` + `core/application/execution_service.py:1194,1202`: both are produced by `parse_context_update(agent.result)`; there is no other source and no `channel` field. **Attribution gotcha:** `DecisionRecorded.aggregate_id` is the **SharedStore** aggregate (0/110 are worker aggregates) — attribute by `payload->>'decided_by'`, never `aggregate_id`. `ArtifactStored.stored_by` IS the worker.
- Run identity = recursive tree over `ChildSpawned{ aggregate_id (parent), payload->>'child_id' }`, roots = `RunStarted` aggregate ids.

### Integrity checks (all passed, via Codex-authored SQL run on live DB)
- `agents_with_multiple_task_assigned = 0`, `agents_with_multiple_distinct_roles = 0` → role attribution unambiguous.
- `duplicate_event_ids = 0`, `duplicate_aggregate_sequence = 0` → no double counting.
- `source_edits_no_valid_edited_by_uuid = 0`, `source_edits_edited_by_no_role = 0` → all 939 edits attributable.
- Tree: 173 roots, 791 edges, `invalid_child_uuid = 0`, `cycle_edges = 0`, `agents_under_multiple_roots = 0`. The 187 "orphans" = 1151 total aggregates − 964 agents = SharedStore aggregates (no `TaskAssigned`); outside the analysis, benign.

---

## SQL + Evidence

Run any block with:
```bash
docker exec -i -e PGPASSWORD=arise arise-db psql -U arise -d arise_events -f - < query.sql
```

### 1. Role distribution (denominators)
```sql
SELECT substring(payload->>'task_description' from '^\[([^]]+)\]') AS role,
       count(DISTINCT aggregate_id) AS agents
FROM events WHERE event_type='TaskAssigned'
GROUP BY 1 ORDER BY 2 DESC;
```
Result (Exploiter roles): Repro-Creator **46**, PoC-Researcher **43**, Exploit-Validator **43**, Forward-Instrumentator **29**, PoC-Tester **28**, Data-Flow-Analyst **15**. (Plus phase managers Exploiter/Builder/Fixer/Reporter = 64 each, and 173 unbracketed = boss roots.)

### 2. Cross-tab: who edits each Exploiter deliverable, by role
```sql
WITH role_map AS (
  SELECT aggregate_id, substring(payload->>'task_description' from '^\[([^]]+)\]') AS role
  FROM events WHERE event_type='TaskAssigned')
SELECT e.payload->>'path' AS path, rm.role,
       count(*) AS edits, count(DISTINCT e.aggregate_id) AS workers
FROM events e JOIN role_map rm ON (e.payload->>'edited_by')::uuid = rm.aggregate_id
WHERE e.event_type='SourceFileEdited'
  AND e.payload->>'path' IN
      ('/testcase/repro.sh','/testcase/poc_path.txt','/testcase/exploit_validation_results.txt')
GROUP BY 1,2 ORDER BY 1, 3 DESC;
```
Result:

```
 /testcase/exploit_validation_results.txt | Repro-Creator      | 21 | 20   <- NON-owner, most
 /testcase/exploit_validation_results.txt | PoC-Tester         | 17 | 17   <- NON-owner
 /testcase/exploit_validation_results.txt | Exploit-Validator  |  9 |  9   <- OWNER
 /testcase/exploit_validation_results.txt | PoC-Researcher     |  7 |  6
 /testcase/exploit_validation_results.txt | Data-Flow-Analyst  |  6 |  5
 /testcase/repro.sh                       | Repro-Creator      | 22 | 22   <- OWNER
 /testcase/repro.sh                       | PoC-Tester         | 17 | 17   <- NON-owner
 /testcase/repro.sh                       | PoC-Researcher     |  9 |  9   <- NON-owner
 /testcase/repro.sh                       | Exploit-Validator  |  5 |  4
 /testcase/repro.sh                       | Data-Flow-Analyst  |  4 |  4
 /testcase/repro.sh                       | Forward-Instrumentator | 2 | 2
 /testcase/repro.sh                       | Build-Compiler     |  1 |  1   <- cross-PHASE bleed
 /testcase/repro.sh                       | Fixer              |  1 |  1   <- cross-PHASE bleed
 /testcase/repro.sh                       | Patch-Creator      |  1 |  1   <- cross-PHASE bleed
 /testcase/poc_path.txt                   | PoC-Researcher     | 24 | 24   <- NON-owner, most
 /testcase/poc_path.txt                   | Repro-Creator      | 14 | 14   <- OWNER
 /testcase/poc_path.txt                   | PoC-Tester         |  4 |  4
 /testcase/poc_path.txt                   | Data-Flow-Analyst  |  1 |  1
 /testcase/poc_path.txt                   | Exploit-Validator  |  1 |  1
 /testcase/poc_path.txt                   | Patch-Creator      |  1 |  1
```
**Owner vs non-owner totals:** `repro.sh` 22 vs 40 · `exploit_validation` 9 vs 51 · `poc_path` 14 vs 31. Non-owners dominate every deliverable.

### 3. Per-run owner abdication (gated on owner being spawned) — the decisive test
```sql
WITH RECURSIVE
edges AS (SELECT aggregate_id AS parent,(payload->>'child_id')::uuid AS child
          FROM events WHERE event_type='ChildSpawned'),
roots AS (SELECT aggregate_id AS root FROM events WHERE event_type='RunStarted'),
tree AS (SELECT root AS agg,root FROM roots
         UNION SELECT e.child,t.root FROM edges e JOIN tree t ON e.parent=t.agg),
role_map AS (SELECT aggregate_id AS agg,
             substring(payload->>'task_description' from '^\[([^]]+)\]') AS role
             FROM events WHERE event_type='TaskAssigned'),
agent_run AS (SELECT t.agg,t.root,rm.role FROM tree t JOIN role_map rm ON t.agg=rm.agg),
run_has_rc AS (SELECT DISTINCT root FROM agent_run WHERE role='Repro-Creator'),
run_has_ev AS (SELECT DISTINCT root FROM agent_run WHERE role='Exploit-Validator'),
ed AS (SELECT (e.payload->>'edited_by')::uuid AS agg, e.payload->>'path' AS path
       FROM events e WHERE e.event_type='SourceFileEdited'),
repro AS (SELECT ar.root, bool_or(ar.role='Repro-Creator') owner_edited,
                 bool_or(ar.role<>'Repro-Creator') nonowner_edited
          FROM ed JOIN agent_run ar ON ed.agg=ar.agg
          WHERE ed.path='/testcase/repro.sh' GROUP BY ar.root),
valid AS (SELECT ar.root, bool_or(ar.role='Exploit-Validator') owner_edited,
                 bool_or(ar.role<>'Exploit-Validator') nonowner_edited
          FROM ed JOIN agent_run ar ON ed.agg=ar.agg
          WHERE ed.path='/testcase/exploit_validation_results.txt' GROUP BY ar.root)
SELECT 'repro.sh (owner=Repro-Creator)' AS deliverable,
  (SELECT count(*) FROM run_has_rc) AS runs_owner_spawned,
  (SELECT count(*) FROM repro) AS runs_file_edited,
  (SELECT count(*) FROM repro WHERE owner_edited) AS owner_edited_it,
  (SELECT count(*) FROM repro WHERE NOT owner_edited AND nonowner_edited) AS only_nonowner_edited
UNION ALL
SELECT 'exploit_validation (owner=Exploit-Validator)',
  (SELECT count(*) FROM run_has_ev),
  (SELECT count(*) FROM valid),
  (SELECT count(*) FROM valid WHERE owner_edited),
  (SELECT count(*) FROM valid WHERE NOT owner_edited AND nonowner_edited);
```
Result:
```
 repro.sh (owner=Repro-Creator)               | runs_owner_spawned 46 | runs_file_edited 50 | owner_edited_it 22 | only_nonowner_edited 28
 exploit_validation (owner=Exploit-Validator) | runs_owner_spawned 43 | runs_file_edited 49 | owner_edited_it  9 | only_nonowner_edited 40
```
Codex's refined version (restricting to runs where owner was spawned AND file edited):
- `repro.sh`: 45 such runs → owner edited 22, **only-non-owner 23 (51%)**.
- `exploit_validation`: 42 such runs → owner edited 9, **only-non-owner 33 (79%)**.

### 4. Retry-immunity (Q3: bleed, not failure-retry)
```sql
SELECT edits_per_worker, count(*) AS num_workers FROM (
  SELECT payload->>'edited_by' AS w, count(*) AS edits_per_worker
  FROM events WHERE event_type='SourceFileEdited' AND payload->>'path'='/testcase/repro.sh'
  GROUP BY 1) x GROUP BY 1 ORDER BY 1;
```
Result: `1 edit → 60 workers`, `2 edits → 1 worker`. Within-worker churn ≈ 0; the redundancy is cross-role.
Note: `secb repro` invocations are **not** in a structured event (`OperationStarted.operation_type` ∈ {`task_assessment`,`worker_execution`,`task_decomposition`} only) — there is no dedicated `SecbCommandExecuted` event. They DO appear as text inside `ThoughtCaptured` (`MCPToolAction`, ~402 rows contain `secb repro`), so they are grep-measurable but not cleanly structured. Either way, the "3× loop" confound is irrelevant to the bleed proof (which rests on cross-role authorship, not invocation counts).

### 5. Context-update channel under-use (Q2)
```sql
WITH role_map AS (SELECT aggregate_id AS agg,
       substring(payload->>'task_description' from '^\[([^]]+)\]') AS role
       FROM events WHERE event_type='TaskAssigned'),
spawned AS (SELECT role, count(*) AS agents FROM role_map GROUP BY role),
-- attribute by decided_by (the worker); NOT aggregate_id (that is the SharedStore aggregate).
decided AS (SELECT rm.role,
       count(DISTINCT (d.payload->>'decided_by')::uuid) AS decision_workers
       FROM events d JOIN role_map rm ON (d.payload->>'decided_by')::uuid=rm.agg
       WHERE d.event_type='DecisionRecorded' GROUP BY rm.role),
-- <output> half of the channel: ArtifactStored.stored_by IS the worker.
arts AS (SELECT rm.role,
       count(DISTINCT (a.payload->>'stored_by')::uuid) AS artifact_workers
       FROM events a JOIN role_map rm ON (a.payload->>'stored_by')::uuid=rm.agg
       WHERE a.event_type='ArtifactStored' GROUP BY rm.role)
SELECT s.role, s.agents,
       COALESCE(d.decision_workers,0) AS decision_workers,
       COALESCE(ar.artifact_workers,0) AS artifact_workers
FROM spawned s LEFT JOIN decided d ON s.role=d.role LEFT JOIN arts ar ON s.role=ar.role
ORDER BY 3 DESC;
```
Result — **EXCERPT** (the SQL returns all 19 roles; shown here are the Exploiter + owner-relevant rows). NOTE: the original doc used `count(DISTINCT aggregate_id)` which is semantically wrong (that is the SharedStore aggregate); the corrected `decided_by` count yields the **same** numbers here only because ≤1 worker per role publishes per run. Combined-publisher % (decision ∪ artifact, distinct workers / spawned) for ALL roles:
```
 role                    decision_workers  artifact_workers  combined%  (of spawned)
 Regression-Tester             2                3            37.5%      /8
 Candidate-Reviewer            1                4            30.8%      /13
 Root-Cause-Analyst           13               15            30.0%      /50
 Patch-Creator                 -               11            22.0%      /50
 PoC-Tester                    6                6            21.4%      /28
 Data-Flow-Analyst             3                3            20.0%      /15
 Build-Setup                   -               10            19.6%      /51
 PoC-Researcher                8                8            18.6%      /43
 Build-Compiler                -                7            13.7%      /51
 Forward-Instrumentator        2                2             6.9%      /29
 Repro-Creator                 3                2             6.5%      /46   <- owner, near-silent
 Build-Verifier                -                2             3.9%      /51
 Exploit-Validator             1                0             2.3%      /43   <- owner, silent
 (Exploiter/Fixer/Builder/Reporter/Patch-Validator/Fix-Aggregator managers: 0%)
```
The "owners publish least" claim is about the **deliverable-owner** roles (Repro-Creator, Exploit-Validator); it is NOT a global "≤30% for every role" claim — Regression-Tester (37.5%) and Candidate-Reviewer (30.8%) exceed 30%.

### 6. Smoking-gun event JSON (cross-role bleed at the event level)
```sql
WITH role_map AS (SELECT aggregate_id AS agg,
       substring(payload->>'task_description' from '^\[([^]]+)\]') AS role
       FROM events WHERE event_type='TaskAssigned')
SELECT jsonb_pretty(jsonb_build_object(
  'editor_role', rm.role, 'edited_path', e.payload->>'path',
  'edited_by', e.payload->>'edited_by', 'event_type', e.event_type))
FROM events e JOIN role_map rm ON (e.payload->>'edited_by')::uuid = rm.agg
WHERE e.event_type='SourceFileEdited'
  AND e.payload->>'path'='/testcase/exploit_validation_results.txt'
  AND rm.role='Repro-Creator' LIMIT 1;
```
Result — a `Repro-Creator` writing the `Exploit-Validator`'s deliverable:
```json
{ "event_type": "SourceFileEdited",
  "edited_path": "/testcase/exploit_validation_results.txt",
  "edited_by": "154370d6-805e-4ac8-95ca-979fcb384bcd",
  "editor_role": "Repro-Creator" }
```

### 7. Robustness: authorship including shell write-redirects (answers "SourceFileEdited misses shell writes")
Parameterize `:path` per deliverable. **Filter `tool_name='MCPToolAction'`, NOT `output_type='tool_use'`** — the latter also matches `FinishAction` final-summary text containing `<decision ...>/testcase/repro.sh`, whose closing `>` is a false positive (repro 4, validation 1 such false hits).
```sql
WITH role_map AS (SELECT aggregate_id AS agg, substring(payload->>'task_description' from '^\[([^]]+)\]') AS role FROM events WHERE event_type='TaskAssigned'),
shell_w AS (SELECT DISTINCT aggregate_id AS agg FROM events
  WHERE event_type='ThoughtCaptured' AND payload->>'tool_name'='MCPToolAction'   -- real shell tool only
    AND payload->>'content' ~ '(>\s*\S*repro\.sh|tee\s+\S*repro\.sh)'),          -- swap repro\.sh -> exploit_validation_results\.txt for the other file
editor_w AS (SELECT DISTINCT (payload->>'edited_by')::uuid AS agg FROM events WHERE event_type='SourceFileEdited' AND payload->>'path'='/testcase/repro.sh'),
any_w AS (SELECT agg FROM shell_w UNION SELECT agg FROM editor_w)
SELECT rm.role, count(DISTINCT ed.agg) editor_authors, count(DISTINCT sh.agg) shell_authors, count(DISTINCT aw.agg) any_authors
FROM role_map rm LEFT JOIN editor_w ed ON ed.agg=rm.agg LEFT JOIN shell_w sh ON sh.agg=rm.agg LEFT JOIN any_w aw ON aw.agg=rm.agg
WHERE rm.role IN ('Repro-Creator','PoC-Researcher','PoC-Tester','Data-Flow-Analyst','Exploit-Validator','Forward-Instrumentator')
GROUP BY rm.role HAVING count(DISTINCT aw.agg)>0 ORDER BY any_authors DESC;
```
Result — owner abdication survives the shell channel (Exploiter-leaf-scoped, `MCPToolAction`-only):
```
 repro.sh:           Repro-Creator(owner) 22 | non-owners 37 (PoC-Tester 17, PoC-Researcher 9, Exploit-Validator ~6, Data-Flow ~4, Forward 2; +3 cross-PHASE: Build-Compiler/Fixer/Patch-Creator if unscoped → 40)
 exploit_validation: Exploit-Validator(owner) 10 | non-owners 49 (Repro-Creator 21, PoC-Tester 17, PoC-Researcher 6, Data-Flow 5)
```
Shell write-redirects via `MCPToolAction` are rare (repro 56, validation 74 raw hits; most `repro.sh` mentions are reads / `secb repro` output / planning text). Conclusion: percentages above are "editor + MCP-shell" authorship; the owner-abdication finding does not depend on the measurement gap.

---

## Root cause (Q1): worker template selected by PHASE, not ROLE

- `plugins/security/prompt_strategy.py:51` maps **every role → its phase branch**; `extend_worker_prompt` (`prompt_strategy.py:~257`) calls `_detect_branch_from_task` and renders the branch template `worker/exploiter.j2`. There is **no per-role template selection**.
- `prompts/domains/secbench/phases/exploit.j2` (included into the worker body) states the **entire phase** as goals 1–6: select PoC → write `poc_path` → create/edit `repro.sh` → run `secb repro` 3× → write `exploit_validation`. **No `{% if role %}` gating exists** in `worker/exploiter.j2` or `phases/exploit.j2`.
- Net: PoC-Researcher and Repro-Creator receive a byte-identical "do the whole phase" body. The worker **does** also receive role-specific context — `prompt_builder.py:160-181` appends the briefing, the manager's subtask justification (plan/search_hints/target_paths), sibling context, and the full bracketed task text. The precise claim is therefore: **there is no role-gated worker template; the long phase-wide imperative body (MUST/MANDATORY) likely dominates the shorter role-specific context.** (This is a strong-but-unproven causal claim about prompt salience, distinct from the proven structural fact that no role gating exists.)

**Clash:** `assess.j2` (manager) → "slice into narrow roles with distinct `produces`/`depends_on`"; `phases/exploit.j2` (worker, via `worker/exploiter.j2`) → "every worker, do the entire phase." The worker body wins.

---

## Corrections to prior analysis
- Exploit-Validator owner edits of its verdict file = **9** (exact path), not 11. The earlier 11 used a `LIKE '/testcase/exploit_validation%'` that swept two stray variant paths (`exploit_validation.txt`, `exploit_validation_report.txt`, 1 each).
- "Many roles edit one file per run" is **wrong**. Within-run multi-role editing is rare (`repro.sh` 20%, `exploit_validation` 16%, `poc_path` 7%; avg ~1.1 roles/file). The real mechanism is **owner abdication**.
- **Codex's automated Q2 check returned 0 and is a false negative**: it filtered `payload->>'channel'='context-update'`, but `DecisionRecorded` has no `channel` field. The code-grounded measure (query 5) is authoritative.

## Reviewer findings adjudicated (2026-06-16)
A later review challenged this doc. Adjudication against the event store:
- **SourceFileEdited ≠ all file production (valid).** Fixed: §Data model caveat + Query 7. Shell writes folded in; owner-abdication holds (corrected in round 2 to `repro.sh` 22/37, `exploit_validation` 10/49). The reviewer's "264 shell-writes" was path *mentions*; actual `MCPToolAction` write-redirects = repro 56 / validation 74.
- **"secb repro not in event store" (valid wording fix).** Corrected at Query 4: absent from `OperationStarted`, present as text in `ThoughtCaptured`; no dedicated structured event.
- **Channel also includes `<output>`→`ArtifactStored` (valid).** Fixed: Query 5 now reports `artifact_workers` too; headline unchanged (owners still near-silent).
- **Query 5 mislabeled `aggregate_id` as worker (valid SQL bug).** Fixed to `decided_by`; numbers identical by coincidence (≤1 publisher/role/run).
- **"Manager creates the layer correctly" overstated (valid).** Softened in BLUF: required roles are under-spawned (64 managers vs 46/15 leaf workers) — the owner role often never exists. This makes manager-side role-spawn/`depends_on` enforcement part of the fix, not optional.
- **"All numbers from event-payload JSON" overbroad (valid, trivial).** Softened in Purpose line.

## Reviewer findings adjudicated — round 2 (2026-06-16)
A third review caught residual numeric/scoping errors. All five verified TRUE against the event store; direction unchanged:
- **Query 7 false positives (valid).** `output_type='tool_use'` also matched `FinishAction` summary text containing `<decision ...>/testcase/repro.sh` (the `>` of the closing tag). Fixed to `tool_name='MCPToolAction'`. Corrected Exploiter-leaf counts: `repro.sh` 22/37 (was 23/39), `exploit_validation` 10/49 (was 10/50).
- **Query 7 only covered repro.sh (valid).** Now parameterized; the validation result is reproducible (swap the path token).
- **"≤30% per role" false globally (valid).** Combined publishers reach 37.5% (Regression-Tester), 30.8% (Candidate-Reviewer). Restated as a claim about deliverable-owner roles only (Query 5 table corrected + full result shown).
- **Query 5 table was an unlabeled excerpt (valid).** Now marked EXCERPT with the full per-role result.
- **Causality overstated "only a bracket" (valid).** Reworded (Root cause §): the worker also gets briefing/subtask/sibling context (`prompt_builder.py:160-181`); the precise claim is "no role-gated template; phase-wide body dominates."
- **Agent-count vs run-count for under-spawn (valid).** BLUF now uses run-level: 57/173 runs have an Exploiter manager; only 15/57 spawn all six required Exploiter leaves.

## Cross-check provenance
- Independent verification SQL authored by Codex (`codex:codex-rescue`); it could not reach the DB from its sandbox, so the queries were executed here against the live event store. They **reproduced cross-tab + per-run + retry numbers exactly** and supplied the owner-spawn gating in query 3.
- Saved query artifacts at time of analysis: `/tmp/role_bleed_verify.sql` (Codex), `/tmp/owner.sql`, `/tmp/bleed2.sql`, `/tmp/q2.sql`, `/tmp/retry.sql` (this session). They are reconstructable from the blocks above.

## Reproduction prerequisites
- Container `arise-db` (`postgres:16-alpine`), DB `arise_events`, user `arise`, password `arise` (`deployment/.env`).
- Connect: `docker exec -i -e PGPASSWORD=arise arise-db psql -U arise -d arise_events`.
