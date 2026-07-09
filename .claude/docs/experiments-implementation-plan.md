# Experiments Implementation Plan

Concrete, PR-by-PR plan to deliver the architecture in
`.claude/docs/experiments-rearchitecture.md`. Each PR is independently
verifiable; rollback = revert that commit. The companion architecture doc is
the *design rationale*; this is the *implementation contract*.

**Branch baseline:** `develop` @ `4827a39` (post-Ollama merge).

---

## 0. Pre-flight (verified against current `develop`)

### Invariants the plan depends on

| Invariant | File | Confirmed? |
|---|---|---|
| `_create_boss_session` is unconditional — BOSS is always instantiated as root | `core/application/execution_service.py:553` | ✅ unchanged since baseline |
| `topology.max_depth ≤ 0` means depth limit *disabled* (unlimited), not zero | `config/settings.py:163`, `core/domain/values/limits.py:27` | ✅ |
| `harness:` field in `manifest.yaml` has no execution-bearing consumer | grep across repo | ✅ — only display use is `experiments/2026-04-23-initial-secbench/scripts/render_report.py:40` |
| `Settings.from_yaml` is single-file (no `extends:` resolution) | `config/settings.py:347` | ✅ |
| Cell config B1/B2/B3 are byte-identical except header comment | `sha256sum` after stripping line 1 | ✅ |
| `register_run.py` writes `(run_id, cell, task, attempt)` into both per-run manifest AND study manifest's `runs:` list | `register_run.py:75, 106` | ✅ |

### Drift since the architecture doc was written

| Change | Affects |
|---|---|
| Base `config/config.yaml` switched to `ollama_chat/kimi-k2.6:cloud` for boss/manager/worker | PR 5 — B-cell overlays must either inherit this or pin different models explicitly |
| New top-level `format_repairer:` config field with its own Pydantic model | PR 3 — strict-mode schema must include it; PR 5 may want B-cell deltas on it |
| `output.directory` changed from `./runs` to `./output` | PR 1 + PR 6 — the runs pool location is now overridable; collect.py / load_runs.py must honor `output.directory` from settings, not hardcode `./runs` |
| Existing legacy runs still in `runs/` (including `runs/_legacy/`); fresh runs land in `./output/` | PR 1 — pool walker must accept multiple roots or read from settings |
| `core/application/agent_orchestrator.py` grew by +567 lines (Qwen/format-repair handling) | PR 4 — mode-flat short-circuit must integrate with new control flow; not just the BOSS-creation site |

### Out of scope (do NOT touch in this plan)

- LLM provider/model selection logic
- Format-repairer behavior (only its schema slot)
- Domain plugin internals (`plugins/security/`)
- Web dashboard (`query/web/`)
- CVE fixture format (`deployment/cve-instances/`)

---

## 1. PR-by-PR breakdown

Each PR section has the same shape:
**Goal · Files · Code changes · Tests · Verification · Risks · Rollback.**

---

### PR 1 — Split inputs from outputs

**Goal:** `manifest.yaml` becomes design-only. Enrollment roster moves into a
derived lockfile under `reports/`. Field rename `attempt` → `replicate`.

#### Files

```
EDIT  experiments/<study>/manifest.yaml          # drop runs:, bump schema_version: 2
EDIT  experiments/shared/scripts/register_run.py # remove _upsert_run_in_study; keep per-run-manifest stamp
EDIT  experiments/shared/scripts/collect.py      # NEW: produce reports/enrollment.lock.yaml
EDIT  experiments/shared/scripts/load_runs.py    # honor settings.output.directory; accept multiple pool roots
EDIT  experiments/shared/scripts/render_report.py  # read enrollment.lock.yaml not manifest.runs
EDIT  experiments/shared/scripts/validate_reports.py  # require enrollment.lock.yaml present and consistent
NEW   experiments/<study>/reports/enrollment.lock.yaml
EDIT  experiments/shared/harness.py              # drop manifest writeback in run_ours / run_baseline
EDIT  experiments/shared/tests/test_harness.py   # update assertions
EDIT  every existing run_manifest.json with attempt: → replicate: (one-shot migration)
```

#### Code changes

**1. Manifest schema bump (study-side):**

```yaml
study_id: 2026-04-23-initial-secbench
schema_version: 2                  # NEW
created_at: 2026-04-23T00:00:00Z
hypothesis: "..."
dataset: dataset.yaml
replicates: 1                      # NEW: default samples per (cell, task)
cells:
  A1: {harness: baseline-claude-code-subagent, config: configs/A1-claude-code-subagent.yaml}
  # cells: still has old fields here; PR 2 rewrites to {group, runner, config}
# runs: REMOVED.
```

**2. `register_run.py`:** delete `_upsert_run_in_study` and its caller in `register_run`.
Keep `_prepare_run_manifest_payload` (the per-run-manifest stamp) — that's
ground-truth, not duplication. Add `replicate: int` parameter where `attempt`
appeared; keep an `attempt` alias accepting the old name for one PR cycle to
make the rename safe.

**3. `collect.py`:** new function `build_enrollment_lock(study_id) → dict`:

```python
def build_enrollment_lock(study_id: str) -> dict:
    pool_roots = _resolve_pool_roots()           # honors settings.output.directory + ./runs
    enrolled: list[dict] = []
    for manifest_path in _iter_run_manifests(pool_roots):
        record = json.loads(manifest_path.read_text())
        if record.get("study_id") != study_id:
            continue
        enrolled.append({
            "run_id":    record["run_id"],
            "cell":      record["cell"],
            "task":      record["task"],
            "replicate": record.get("replicate", record.get("attempt", 0)),
        })
    enrolled.sort(key=lambda r: (r["cell"], r["task"], r["replicate"], r["run_id"]))
    return {
        "study_id":    study_id,
        "rendered_at": _utcnow_iso(),
        "design_sha":  _git_sha_of(_manifest_path(study_id)),
        "enrollment": enrolled,
    }
```

`_resolve_pool_roots()` walks BOTH `./runs/` (legacy + `_legacy/`) AND
`settings.output.directory` if different — until a future PR consolidates.

**4. `render_report.py`:** swap `manifest.get("runs")` for the loaded
`enrollment.lock.yaml`.

**5. `validate_reports.py`:** assert (a) `enrollment.lock.yaml` exists, (b) every
`run_id` in the lockfile resolves to a `run_manifest.json` in some pool root,
(c) the `manifest.yaml` does NOT contain a `runs:` key.

#### Tests

```
NEW  experiments/shared/tests/test_collect_enrollment.py
NEW  experiments/shared/tests/test_register_run_no_manifest_writeback.py
EDIT experiments/shared/tests/test_harness.py       # drop manifest-writeback expectations
```

Coverage:
- Lockfile is sorted deterministically (`(cell, task, replicate, run_id)`).
- Lockfile excludes runs from other `study_id`s.
- Pool walker finds runs in both `runs/` and `output/`.
- Validator rejects `manifest.yaml` containing `runs:`.
- Rename: a legacy `run_manifest.json` with `attempt:` deserializes as `replicate:`.

#### Verification

```bash
# 1. Schema migration: existing study still loads
uv run python -m experiments.shared.scripts.collect --study 2026-04-23-initial-secbench

# 2. Lockfile entries match what manifest.yaml's runs: list contained
uv run python -c "
import yaml
old = yaml.safe_load(open('/tmp/manifest_pre_pr1.yaml')).get('runs', [])
new = yaml.safe_load(open('experiments/2026-04-23-initial-secbench/reports/enrollment.lock.yaml'))['enrollment']
assert {(r['run_id'], r['cell'], r['task']) for r in old} \
       == {(r['run_id'], r['cell'], r['task']) for r in new}
print('OK')
"

# 3. Report.md unchanged byte-for-byte vs pre-PR baseline
uv run python -m experiments.shared.scripts.render_report --study 2026-04-23-initial-secbench
diff /tmp/report_pre_pr1.md experiments/2026-04-23-initial-secbench/reports/report.md
```

#### Risks

| Risk | Mitigation |
|---|---|
| Pool drift: legacy runs in `runs/`, new runs in `output/` produce inconsistent lockfile | Walker reads both roots; deduplicate on `run_id`; flag duplicates as ERROR |
| `attempt` → `replicate` rename breaks tests reading raw JSON | One-PR-cycle alias; CI runs both old and new fixture forms |
| Existing reports stop validating because lockfile has different field names | Add lockfile generation as a `prepare:` step before `validate_reports` runs in CI |

#### Rollback

`git revert` the PR. Lockfile generation is purely derived from the runs pool; reverting recreates `manifest.yaml.runs` from the lockfile if a one-shot rebuild script is preserved.

---

### PR 2 — Groups + runner registry + manifest validator

**Goal:** Make manifest cells `{group, runner, config}`. Introduce
project-wide `groups.yaml`. Stub the runner registry with one entry (`aris`)
that delegates to today's `harness.run_ours` for B-cells and a thin
adapter calling `run_baseline` for A-cells. **No semantic change yet** —
this is purely the manifest/dispatch refactor.

#### Files

```
NEW   experiments/shared/groups.yaml
NEW   experiments/shared/runners/__init__.py     # registry + auto-discovery
NEW   experiments/shared/runners/aris.py         # default runner; delegates to existing harness fns
NEW   experiments/shared/scripts/validate_manifest.py
EDIT  experiments/<study>/manifest.yaml          # rewrite cells to {group, runner, config}
EDIT  experiments/<study>/scripts/render_report.py  # display via groups.yaml lookup, not entry["harness"]
EDIT  experiments/shared/harness.py              # accept (cell, runner) pair from manifest, not CLI
EDIT  pre-commit config (.pre-commit-config.yaml)  # add validate-experiment-manifests hook
```

#### Code changes

**1. `experiments/shared/groups.yaml`:**

```yaml
A: Claude Code CLI
B: Our System
```

**2. `experiments/shared/runners/__init__.py`:**

```python
@runtime_checkable
class Runner(Protocol):
    id: str
    label: str
    def run(self, *, study_id: str, cell: str, task: str,
            replicate: int, config: Path, context_file: Path) -> UUID: ...

_REGISTRY: dict[str, Runner] = {}

def register(r: Runner) -> Runner: ...
def get(rid: str) -> Runner: ...
def all_ids() -> list[str]: ...

# At end of module: pkgutil.iter_modules(__path__) → import each, triggering registration.
```

**3. `experiments/shared/runners/aris.py`** (PR 2 version is a *shim*; PR 4 rewrites it to use the merged config + invariant layer):

```python
class _ArisShim:
    id = "aris"
    label = "Our System"
    def run(self, *, study_id, cell, task, replicate, config, context_file) -> UUID:
        # PR 2: figure out flat-vs-hierarchical from cell name prefix
        # (temporary; PR 4 replaces with orchestration.mode reading)
        if cell.startswith("A"):
            return harness.run_baseline(study_id=study_id, cell=cell, task=task,
                                        attempt=replicate, variant=_legacy_variant(cell))
        return harness.run_ours(study_id=study_id, cell=cell, task=task,
                                attempt=replicate, config=config)

register(_ArisShim())
```

**4. `validate_manifest.py`:**

```python
_GROUP_KEY_RE = re.compile(r"^[A-Z]$")

def validate_manifest(manifest: dict, *, repo_root: Path) -> None:
    groups   = _load_groups(repo_root)
    runners  = set(_runners_module().all_ids())
    errors: list[str] = []

    for cell_name, spec in (manifest.get("cells") or {}).items():
        for k in ("group", "runner", "config"):
            if k not in spec: errors.append(f"cells.{cell_name}: missing {k!r}")

        if (g := spec.get("group")) and g not in groups:
            errors.append(f"cells.{cell_name}.group = {g!r} not in groups.yaml")

        if g and not cell_name.startswith(g):
            errors.append(f"cells.{cell_name}: name must start with group letter {g!r}")

        if (r := spec.get("runner")) and r not in runners:
            errors.append(f"cells.{cell_name}.runner = {r!r} not registered")

        if (c := spec.get("config")) and not (repo_root / "experiments" / manifest["study_id"] / c).is_file():
            errors.append(f"cells.{cell_name}.config not found: {c}")

    if errors: raise ValueError("manifest validation failed:\n  - " + "\n  - ".join(errors))
```

**5. Manifest rewrite:**

```yaml
cells:
  A1: {group: A, runner: aris, config: configs/A1-claude-code-subagent.yaml}
  A2: {group: A, runner: aris, config: configs/A2-claude-code-nosubagent.yaml}
  B1: {group: B, runner: aris, config: configs/B1-ours-naive.yaml}
  B2: {group: B, runner: aris, config: configs/B2-ours-cybersec.yaml}
  B3: {group: B, runner: aris, config: configs/B3-ours-full.yaml}
```

`harness:` field is removed. `render_report.py:40` is updated to display `f"{groups[spec['group']]} / {spec['runner']}"` instead.

#### Tests

```
NEW experiments/shared/tests/test_validate_manifest.py
NEW experiments/shared/tests/test_runners_registry.py
EDIT existing render_report tests for the display swap
```

Coverage:
- Validator: missing field, unknown group, unknown runner, missing config file, group/cell-name mismatch.
- Registry: collision raises; `get` on unknown id raises with available list; auto-discovery picks up modules; explicit `register()` works in tests.
- A run via `runners.get("aris").run(...)` produces the same `run_id`-and-artifacts behavior as a direct `harness.run_ours` / `run_baseline` call.

#### Verification

```bash
uv run python -m experiments.shared.scripts.validate_manifest \
    --study 2026-04-23-initial-secbench   # must succeed

# Negative tests
git stash && yq '.cells.A1.group = "Z"' -i experiments/.../manifest.yaml
uv run python -m experiments.shared.scripts.validate_manifest --study 2026-04-23-initial-secbench
# must FAIL with "Z not in groups.yaml"
git stash pop

# A1 run via the new registry path produces equivalent artifacts to direct harness call
diff <(jq -S . runs/<a1-via-registry>/run_manifest.json) \
     <(jq -S . runs/<a1-via-direct>/run_manifest.json)
# Differences allowed: run_id, started_at, ended_at, git_sha. NOT allowed: shape, kind, exit_status, variant.
```

#### Risks

| Risk | Mitigation |
|---|---|
| `_legacy_variant(cell)` mapping (cell name → baseline variant) is brittle | Make it explicit per cell in manifest as a temporary `legacy_variant:` field; PR 4 deletes it |
| Auto-discovery imports break when adding broken runner | Wrap each import in `try/except ImportError` with logging; broken runner just isn't available |
| `harness:` removal breaks `render_report.py` mid-rebase | Land display-side change in the same PR |

#### Rollback

Revert. Manifest reverts to old shape; renderer reverts. Registry module stays harmless if unused.

---

### PR 3 — Schema + overlay resolver (THE GATE)

**Goal:** `Settings.from_yaml` learns to resolve `extends:` + `overrides:`.
New Pydantic models: `orchestration.mode`, `worker.tool_params` (tagged
union keyed by `worker.tool`), `domain.plugin`, `domain.params`, plus strict
`extra="forbid"` at every level. **No behavior change** — all existing
configs still load and produce the same effective Settings. New fields have
sensible defaults.

This PR is the gate: PRs 4–6 cannot land without it.

#### Files

```
NEW   config/overlay.py                    # extends/overrides resolver
EDIT  config/settings.py                   # add new fields; bump strict mode
EDIT  config/config.yaml                   # explicit defaults for new fields (orchestration.mode: hierarchical etc.)
EDIT  config/tests/test_settings.py
NEW   config/tests/test_overlay.py
EDIT  presentation/persistence/run_persistence.py  # already records invocation_sha256; ensure it covers new fields
```

#### Code changes

**1. `config/overlay.py`:**

```python
def resolve_overlay(path: Path, *, repo_root: Path) -> dict:
    """Load a YAML; if it has `extends:`, recursively merge its `overrides:`
    onto the base. Returns the materialized config dict, ready for
    Settings._build_from_config."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: must be a mapping")

    if "extends" in raw or "overrides" in raw:
        if not ("extends" in raw and "overrides" in raw):
            raise ValueError(f"{path}: extends/overrides must appear together")
        base_rel = raw["extends"]
        base = resolve_overlay(repo_root / base_rel, repo_root=repo_root)
        return _deep_merge(base, _flatten_dotted(raw["overrides"]))

    return raw


def _flatten_dotted(d: dict) -> dict:
    """{"a.b.c": v}  →  {"a": {"b": {"c": v}}}"""
    ...

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive merge; override wins; lists are replaced not concatenated."""
    ...
```

**2. `Settings.from_yaml`:**

```python
@classmethod
def from_yaml(cls, config_path: str | Path) -> "Settings":
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    config = resolve_overlay(config_file, repo_root=get_repo_root())
    return cls._build_from_config(config)
```

**3. New Pydantic models** (`config/settings.py`):

```python
class OrchestrationConfig(BaseModel):
    model_config = {"extra": "forbid"}
    mode: Literal["hierarchical", "flat"] = "hierarchical"   # NEW; default preserves today's behavior
    # ... existing fields ...

class ClaudeCodeParams(BaseModel):
    model_config = {"extra": "forbid"}
    allowed_tools:    list[str] = ["*"]
    disallowed_tools: list[str] = []
    output_format:    Literal["stream-json", "text"] = "stream-json"
    include_partial_messages: bool = True
    max_turns:        int = 40

class OpenHandsParams(BaseModel):
    model_config = {"extra": "forbid"}
    image:                 str = "openhands:latest"
    timeout_seconds:       int = 600
    max_iterations_per_run: int = 20

class WorkerToolParams(BaseModel):
    """Tagged union; only the matching block is populated."""
    model_config = {"extra": "forbid"}
    claude_code: ClaudeCodeParams | None = None
    openhands:   OpenHandsParams   | None = None
    google_adk:  dict | None       = None  # placeholder

class WorkerConfig(BaseModel):
    model_config = {"extra": "forbid"}
    tool: Literal["claude_code", "openhands", "google_adk"] = "openhands"
    tool_params: WorkerToolParams = WorkerToolParams()
    # ... existing fields preserved ...

class DomainConfig(BaseModel):
    model_config = {"extra": "forbid"}
    plugin: Literal["security"] | None = None
    params: dict[str, Any] = {}      # validated by the plugin, not core

class Settings(BaseSettings):
    model_config = {"extra": "forbid", "env_nested_delimiter": "__"}
    # ... existing fields ...
    domain: DomainConfig = DomainConfig()                    # NEW
    format_repairer: FormatRepairerConfig = FormatRepairerConfig()  # already added on develop

    @model_validator(mode="after")
    def _validate_tool_params_match_tool(self) -> "Settings":
        """Reject configs where worker.tool_params.<tool> is None for the selected worker.tool."""
        ...
```

**4. `config/config.yaml`:** add explicit defaults so existing behavior is preserved:

```yaml
orchestration:
  mode: hierarchical                  # default; flat is opt-in via overlay
  # … rest unchanged …

worker:
  tool: openhands
  tool_params:
    openhands:
      image: openhands:latest
      timeout_seconds: 1000
      max_iterations_per_run: 20
  # … rest unchanged …

domain:
  plugin: security                    # current ours behavior
  params: {}
```

#### Tests

```
NEW config/tests/test_overlay.py:
  - extends + overrides loads correctly
  - dotted keys flatten correctly: "a.b.c": v
  - deep_merge: override wins; lists replaced
  - missing extends pair (extends without overrides) raises
  - relative extends path resolves from repo_root
  - overlay can extend an overlay (chain)

EDIT config/tests/test_settings.py:
  - extra="forbid": unknown top-level field raises
  - extra="forbid": unknown nested field raises (worker.tool_params.claude_code.foo)
  - tool_params validation: tool=openhands but tool_params.openhands is None → raises
  - All current B configs (rewritten as overlays in PR 5) parse cleanly
```

#### Verification

```bash
# 1. Existing config/config.yaml still loads with no errors
uv run python -c "from config.settings import Settings; s = Settings.from_yaml('config/config.yaml'); print(s.orchestration.mode, s.worker.tool)"

# 2. Strict mode catches stray fields
echo "garbage_field: 1" >> /tmp/test.yaml
cat config/config.yaml >> /tmp/test.yaml
uv run python -c "from config.settings import Settings; Settings.from_yaml('/tmp/test.yaml')" 2>&1 | grep -q "extra" && echo OK

# 3. Tagged union validates
yq '.worker.tool = "claude_code"' config/config.yaml > /tmp/bad.yaml
uv run python -c "from config.settings import Settings; Settings.from_yaml('/tmp/bad.yaml')" 2>&1 | grep -q "tool_params.claude_code" && echo OK

# 4. Effective config from a B1 overlay (PR 5 produces this)
uv run python -m config.overlay --resolve experiments/.../configs/B1-ours-naive.yaml | head -30
```

#### Risks

| Risk | Mitigation |
|---|---|
| Strict mode rejects fields someone uses but never declared | Run `from_yaml` over every committed `*.yaml` in CI; allowlist or add models for any survivors |
| `_deep_merge` semantics for lists are surprising (replace vs append) | Document explicitly; default to replace; provide an opt-in `__append` marker key |
| Tagged union (`tool_params.<tool>`) clashes with dotted-key flatten | Test: `worker.tool_params.openhands.timeout_seconds: 600` must resolve to the right slot |
| Env-var overrides via Pydantic Settings now interact with overlays | Document: env vars override the *resolved* config, applied last |

#### Rollback

Revert. Old `Settings.from_yaml` returns; new fields stop being read. Overlays written under PR 5 stop loading until reapplied.

---

### PR 4 — Invariant layer + WorkerPort + `orchestration.mode: flat`

**Goal:** Centralize task prompt / tool policy / timeouts / env / workspace
in `core/application/run_invariants.py`. Add `ClaudeCodeWorker` behind
`WorkerPort`. Implement `mode=flat` short-circuit in `ExecutionService`. Old
`run_claude_code.py` becomes a thin shim that goes through main.py with a
flat overlay; deletion follows in PR 5.

#### Files

```
NEW   core/application/run_invariants.py
NEW   core/ports/worker_port.py            # if not already present at this abstraction
NEW   infrastructure/workers/claude_code_worker.py
EDIT  infrastructure/workers/openhands_worker.py   # conform to refined WorkerPort
EDIT  core/application/execution_service.py        # branch on orchestration.mode
EDIT  core/application/agent_orchestrator.py       # propagate mode through dispatcher
EDIT  experiments/shared/runners/aris.py           # remove cell-prefix branching; let mode handle it
NEW   tests/experiments/test_invariants.py
NEW   tests/integration/test_flat_mode_baseline.py
EDIT  experiments/shared/baselines/run_claude_code.py  # shim that delegates to main.py + overlay
```

#### Code changes

**1. `core/application/run_invariants.py`:**

```python
@dataclass(frozen=True)
class TaskPromptSpec:
    rendered_prompt: str
    briefing_sha:    str          # for provenance
    cve_context:     dict | None

@dataclass(frozen=True)
class ToolPolicy:
    allowed:              tuple[str, ...]
    disallowed:           tuple[str, ...]
    allowed_bash_commands: tuple[str, ...]    # for claude_code settings.json render

@dataclass(frozen=True)
class TimeoutBudget:
    per_worker_call:      int
    per_run_total:        int

@dataclass(frozen=True)
class WorkspaceSpec:
    root:                 Path
    src_dir:              Path
    testcase_dir:         Path
    secb_available:       bool

def build_task_prompt(*, briefing_path: Path, cve_context: dict | None,
                      task: str) -> TaskPromptSpec: ...

def build_tool_policy(*, settings: Settings, mode: str) -> ToolPolicy: ...

def build_timeouts(settings: Settings) -> TimeoutBudget: ...

def build_env_policy() -> frozenset[str]: ...    # the existing baseline allowlist

def build_workspace_spec(*, settings: Settings, cve_context: dict,
                          run_dir: Path) -> WorkspaceSpec: ...
```

**2. `infrastructure/workers/claude_code_worker.py`:**

```python
class ClaudeCodeWorker:
    def __init__(self, *, params: ClaudeCodeParams, env_policy: frozenset[str]) -> None: ...

    async def run_task(self, *, spec: TaskPromptSpec, tool_policy: ToolPolicy,
                       timeouts: TimeoutBudget, workspace: WorkspaceSpec) -> WorkerResult:
        scratch = self._materialize_settings_dir(tool_policy)
        argv    = self._build_argv(tool_policy=tool_policy, prompt=spec.rendered_prompt)
        env     = self._build_env(scratch, env_policy=self._env_policy)
        return await self._exec_claude(argv, env=env, timeout=timeouts.per_worker_call,
                                        cwd=workspace.root)

    def _materialize_settings_dir(self, policy: ToolPolicy) -> Path:
        scratch = Path(tempfile.mkdtemp(prefix="claude-settings-"))
        (scratch / "settings.json").write_text(json.dumps({
            "permissions": {
                "allow": [f"Bash({c})" for c in policy.allowed_bash_commands],
            },
        }, indent=2))
        return scratch
```

**3. `ExecutionService` flat-mode short-circuit:**

```python
async def execute(self, task_description: str, ...) -> ...:
    if self._config.orchestration.mode == "flat":
        return await self._execute_flat(task_description, ...)
    return await self._execute_hierarchical(task_description, ...)

async def _execute_flat(self, task_description: str, ...) -> ...:
    """Skip BOSS/MANAGER. Build invariants, call WorkerPort directly. Emit a
    minimal event stream: SubtaskSpawned(worker, root) → WorkerStarted →
    WorkerOutput → WorkerCompleted → RunFinalized."""
    ...
```

**4. `aris.py` rewrite:**

```python
class _Aris:
    id = "aris"
    label = "Our System"

    def run(self, *, study_id, cell, task, replicate, config, context_file) -> UUID:
        # 1. resolve overlay → effective Settings
        settings  = Settings.from_yaml(config)
        # 2. invoke main.py via subprocess (process isolation matches today's pattern)
        run_id = _invoke_main_py(config=config, task=task, context_file=context_file)
        # 3. stamp study_id + cell + task + replicate into runs/<run_id>/run_manifest.json
        _stamp_per_run_manifest(run_id, study_id, cell, task, replicate)
        # 4. snapshot effective config for provenance
        _snapshot_effective_config(run_id, settings)
        return run_id
```

#### Tests

```
NEW tests/experiments/test_invariants.py:
  - test_task_prompt_byte_identical_across_modes
  - test_tool_policy_renders_to_claude_cli_flags
  - test_tool_policy_renders_to_openhands_allowlist
  - test_env_policy_strips_secrets_in_both_modes
  - test_workspace_spec_consistent_across_modes

NEW tests/integration/test_flat_mode_baseline.py:
  - end-to-end: mode=flat + worker=claude_code on a tiny CVE produces a
    run_manifest.json with the same invariants as the legacy run_claude_code.py

EDIT existing harness tests:
  - assert run_baseline still works via the shim (until PR 5 deletes it)
```

#### Verification

```bash
# 1. Invariant tests green
uv run pytest tests/experiments/test_invariants.py -v

# 2. End-to-end: A1 via new flat mode
uv run python main.py -c experiments/.../configs/A1-claude-code-subagent.yaml \
    run gpac.cve-2021-40575 \
    --domain-context-file deployment/cve-instances/gpac-cve-2021-40575.json
# Inspect: runs/<uuid>/run_manifest.json should have kind: orchestration_run
# (NOT kind: claude_code_baseline; that's the migration goal — see "normalized comparison")

# 3. Normalized comparison vs legacy
uv run python -m experiments.shared.tests.compare_normalized \
    --new-run runs/<a1-via-flat>/run_manifest.json \
    --old-run runs/<a1-via-legacy>/run_manifest.json
# Must agree on: exit_status, git_sha, worker tool, tokens±5%, wall_time±10%
```

#### Risks

| Risk | Mitigation |
|---|---|
| `_execute_flat` must produce the same event store shape downstream consumers (collect.py, dashboard) expect | Unit-test event sequence; integration-test that collect.py picks up flat-mode runs |
| Workspace prep currently lives inside `SecurityDomainPlugin` and needs to fire BEFORE worker invocation in flat mode too | Extract `prepare_workspace` to a domain hook called by ExecutionService regardless of mode |
| ClaudeCodeWorker materialized `settings.json` may conflict with user's `~/.claude/settings.json` | Set `CLAUDE_CONFIG_DIR=<scratch>` in subprocess env to fully isolate |
| `WorkerPort` change breaks existing OpenHands wiring | Adapt `OpenHandsWorker` in the same PR; keep all changes atomic |

#### Rollback

Revert. `aris.py` reverts to PR 2 shim. Flat mode unavailable but baselines still run via legacy shim until PR 5.

---

### PR 5 — Real B-cell deltas + delete legacy baselines

**Goal:** Rewrite all five cell configs as overlays expressing the actual
experimental hypothesis. Delete `experiments/shared/baselines/`. Resolve
"Q1: what does naive/cybersec/full mean" with concrete deltas.

**Pre-req:** answer Q1 (see §3 below). Do NOT start until those deltas are
declared.

#### Files

```
EDIT  experiments/<study>/configs/A1-claude-code-subagent.yaml     # to overlay form
EDIT  experiments/<study>/configs/A2-claude-code-nosubagent.yaml   # to overlay form
EDIT  experiments/<study>/configs/B1-ours-naive.yaml               # to overlay form + real deltas
EDIT  experiments/<study>/configs/B2-ours-cybersec.yaml            # to overlay form + real deltas
EDIT  experiments/<study>/configs/B3-ours-full.yaml                # to overlay form + real deltas
DELETE experiments/shared/baselines/run_claude_code.py
DELETE experiments/shared/baselines/run_claude_code_no_subagent.py
DELETE experiments/shared/baselines/__init__.py (if empty after deletes)
EDIT  experiments/shared/harness.py                                # remove run-baseline subcommand
EDIT  experiments/shared/runners/aris.py                           # remove legacy fallback path
```

#### Code changes (template — actual deltas pending Q1 resolution)

```yaml
# A1-claude-code-subagent.yaml
extends: config/config.yaml
overrides:
  orchestration.mode: flat
  worker.tool: claude_code
  worker.tool_params.claude_code.allowed_tools: ["*"]
  worker.tool_params.claude_code.disallowed_tools: []
  domain.plugin: null

# A2-claude-code-nosubagent.yaml
extends: config/config.yaml
overrides:
  orchestration.mode: flat
  worker.tool: claude_code
  worker.tool_params.claude_code.disallowed_tools: ["Task"]
  domain.plugin: null

# B1-ours-naive.yaml — TBD pending Q1 (placeholder shape)
extends: config/config.yaml
overrides:
  orchestration.mode: hierarchical
  worker.tool: openhands
  domain.plugin: null                                # naive: no security scaffolding

# B2-ours-cybersec.yaml — TBD
extends: config/config.yaml
overrides:
  orchestration.mode: hierarchical
  worker.tool: openhands
  domain.plugin: security
  domain.params:
    toolsets:
      recon:
        enabled_for_roles: [boss, manager]
        allowed_tools: [search_codebase, read_file, get_file_structure]

# B3-ours-full.yaml — TBD
extends: config/config.yaml
overrides:
  orchestration.mode: hierarchical
  worker.tool: openhands
  orchestration.topology.max_depth: 4
  orchestration.topology.max_total_agents: 60
  domain.plugin: security
  domain.params:
    toolsets:
      recon:
        enabled_for_roles: [boss, manager, pending]
        allowed_tools:
          [search_codebase, read_file, get_file_structure,
           get_symbols_overview, read_symbol]
```

#### Tests

```
EDIT existing config-parse tests to load each overlay and assert non-empty deltas
NEW  test that diff(A1, A2) is exactly {disallowed_tools}
NEW  test that diff(B1, B2) is non-empty AND mentions the domain plugin
NEW  test that diff(B2, B3) is non-empty AND mentions either topology or domain.params
```

#### Verification

```bash
# 1. Each cell config resolves to a Settings without errors
for f in experiments/.../configs/*.yaml; do
    uv run python -c "from config.settings import Settings; Settings.from_yaml('$f')" || exit 1
done

# 2. Diffs are non-trivial
diff <(uv run python -m config.overlay --resolve experiments/.../configs/B1-ours-naive.yaml) \
     <(uv run python -m config.overlay --resolve experiments/.../configs/B2-ours-cybersec.yaml) \
     | grep -v "^---\|^+++\|^@@" | wc -l
# > 0  (must show real deltas)

# 3. Smoke run on one CVE per cell
uv run python -m experiments.shared.scripts.run_matrix \
    --study 2026-04-23-initial-secbench --cells A1,B1 --tasks gpac.cve-2021-40575 --replicates 1
```

#### Risks

| Risk | Mitigation |
|---|---|
| Q1 wasn't fully resolved → B-cell deltas are still ambiguous | Block PR 5 on a written agreement (in `.claude/docs/experiments-rearchitecture.md` §7) before coding |
| Deleting `experiments/shared/baselines/` breaks any external tooling that imported it | Grep for cross-imports before deletion; one full sweep for `from experiments.shared.baselines` |
| Cell overlays drift from base `config/config.yaml` if the base evolves | Effective config snapshot captured per-run (PR 6 ships this); audit job compares snapshots periodically |

#### Rollback

Revert. Cell overlays revert to PR 2 form. Baseline modules return; legacy fallback in `aris.py` re-engages.

---

### PR 6 — Matrix driver + provenance + pre-commit hooks

**Goal:** Ship `run_matrix.py` end-to-end. Record `uv.lock` sha and
`effective_config.yaml` per run. Hook `validate_manifest` and
`validate_reports` into pre-commit. **One command produces a complete
matrix report.**

#### Files

```
NEW   experiments/shared/scripts/run_matrix.py
EDIT  presentation/persistence/run_persistence.py    # add uv_lock_sha256
NEW   infrastructure/snapshot.py                     # write effective_config.yaml
EDIT  .pre-commit-config.yaml                        # validate-experiment-manifests + reports
NEW   experiments/<study>/reports/matrix-summary.md   # per-run skipped/failed/succeeded breakdown
EDIT  experiments/shared/scripts/collect.py          # picks up effective_config.yaml in lockfile
```

#### Code changes

**1. `run_matrix.py`:**

```python
def main(study: str,
         cells_filter: list[str] | None = None,
         tasks_filter: list[str] | None = None,
         replicates: int | None = None,
         parallel: int = 1,
         continue_on_error: bool = True) -> int:
    repo_root = get_repo_root()
    manifest_path = repo_root / "experiments" / study / "manifest.yaml"
    manifest      = yaml.safe_load(manifest_path.read_text())
    validate_manifest(manifest, repo_root=repo_root)

    dataset = yaml.safe_load((manifest_path.parent / manifest["dataset"]).read_text())
    n_replicates = replicates or manifest.get("replicates", 1)

    jobs: list[tuple[str, str, int]] = []
    for cell_name, spec in manifest["cells"].items():
        if cells_filter and cell_name not in cells_filter:
            continue
        for task in (tasks_filter or _resolve_effective_cves(dataset, cell_name)):
            for rep in range(n_replicates):
                jobs.append((cell_name, task, rep))

    results = _dispatch_jobs(jobs, manifest=manifest, parallel=parallel,
                              continue_on_error=continue_on_error)

    # Post-execution pipeline
    collect.build_enrollment_lock(study)
    plot_success.main(study)
    render_report.main(study)
    validate_reports.main(study)
    _write_matrix_summary(results, study)

    failed = [r for r in results if r.failed]
    return 0 if not failed else 1


def _dispatch_jobs(jobs, *, manifest, parallel, continue_on_error) -> list[JobResult]:
    if parallel <= 1:
        return [_run_one(j, manifest, continue_on_error) for j in jobs]
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = [ex.submit(_run_one, j, manifest, continue_on_error) for j in jobs]
        return [f.result() for f in concurrent.futures.as_completed(futures)]
```

**2. Provenance in `run_persistence.py`:**

```python
payload["uv_lock_sha256"] = _uv_lock_sha()
payload["effective_config_path"] = str(run_dir / "effective_config.yaml")

def _uv_lock_sha() -> str | None:
    p = get_repo_root() / "uv.lock"
    if not p.is_file(): return None
    return hashlib.sha256(p.read_bytes()).hexdigest()
```

**3. `infrastructure/snapshot.py`:**

```python
def snapshot_effective_config(*, run_id: UUID, settings: Settings) -> Path:
    target = _runs_pool_root() / str(run_id) / "effective_config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False))
    return target
```

**4. `.pre-commit-config.yaml`:**

```yaml
- id: validate-experiment-manifests
  name: validate experiment manifests
  entry: uv run python -m experiments.shared.scripts.validate_manifest --all-studies
  language: system
  files: ^experiments/.*/manifest\.yaml$|^experiments/shared/groups\.yaml$|^experiments/shared/runners/

- id: validate-experiment-reports
  name: validate experiment reports
  entry: uv run python -m experiments.shared.scripts.validate_reports --all-studies
  language: system
  files: ^experiments/.*/reports/
```

#### Tests

```
NEW experiments/shared/tests/test_run_matrix.py:
  - empty filter runs full matrix
  - --cells filter respected
  - --tasks filter respected
  - --replicates overrides manifest default
  - continue_on_error captures failures and continues
  - parallel=4 runs concurrently (verify by timing or by mock dispatcher counting overlap)
  - exit code reflects success (0) vs any failure (1)
  - post-execution pipeline runs in correct order: collect → plot → render → validate

NEW tests for snapshot and uv-lock recording
```

#### Verification

```bash
# 1. End-to-end on the actual study
uv run python -m experiments.shared.scripts.run_matrix \
    --study 2026-04-23-initial-secbench --replicates 1

# 2. Check provenance
ls experiments/2026-04-23-initial-secbench/reports/
# enrollment.lock.yaml, report.md, tables/, figures/, matrix-summary.md

# 3. Verify per-run effective config + uv lock sha
jq '.uv_lock_sha256, .effective_config_path' \
    runs/$(ls -1 runs/ | head -1)/run_manifest.json
ls runs/$(ls -1 runs/ | head -1)/effective_config.yaml

# 4. Pre-commit triggers
echo "garbage_cell: {}" >> experiments/.../manifest.yaml
git add experiments/.../manifest.yaml && pre-commit run validate-experiment-manifests
# must FAIL
git checkout -- experiments/.../manifest.yaml
```

#### Risks

| Risk | Mitigation |
|---|---|
| `--parallel N` causes Docker daemon contention | Cap at 4 per docs; smoke-test parallelism in CI; advise sequential default |
| Pre-commit hooks slow down commits | Make them fast: validator runs in <1s on a typical study |
| `effective_config.yaml` accidentally captures secrets | `Settings.model_dump(mode="json")` should exclude secrets via Pydantic `SecretStr`; audit the dump |
| `uv.lock` not present in some clones | `_uv_lock_sha` returns None and field is omitted; downstream consumers tolerate missing |

#### Rollback

Revert. Matrix driver disappears; users fall back to manual per-cell harness invocations from PR 4.

---

## 2. Cross-cutting concerns

### 2.1 Field rename: `attempt` → `replicate`

- **Schema:** PR 1 introduces `replicate` parameter; alias `attempt` accepted for one cycle.
- **Semantics:** 0-indexed monotonic per `(cell, task)`. Enforced uniqueness on the triple in `register_run.py`.
- **Existing data:** one-shot script (PR 1) mutates every `runs/<uuid>/run_manifest.json` to add `replicate` field, copying from `attempt`.
- **Removal of `attempt`:** PR 6 deletes the alias.

### 2.2 Pool location: `runs/` vs `output/`

- Base `config/config.yaml` now sets `output.directory: ./output`.
- Legacy data lives in `runs/` (including `runs/_legacy/`).
- **PR 1 decision:** `_resolve_pool_roots()` reads `settings.output.directory` AND walks `./runs/` if it exists. Both are scanned. Duplicate `run_id` between roots is a hard error.
- **PR 6 decision:** `run_matrix.py` writes new runs to whatever `settings.output.directory` resolves to (per-cell, since each cell's overlay can override).
- **Long-term cleanup:** a follow-up PR (out of scope) consolidates to a single root.

### 2.3 Field consumed-or-deleted audit (Pydantic strict mode)

When PR 3 lands, run a one-shot scan: load every committed YAML through `Settings.from_yaml`. Any `extra` rejection is either:
- A field with no consumer in code → **delete from YAML** (this includes the current A1/A2 `variant:` and `notes:` documentation fields).
- A field with a real consumer that wasn't modeled → **add to Pydantic schema with type and validation**.

Track in `experiments/shared/scripts/audit_dead_fields.py` (one-shot script, retained for future audits).

### 2.4 CI gating

After PR 6:
- Pre-commit hooks: `validate-experiment-manifests`, `validate-experiment-reports`, `architecture-boundary-check` (existing).
- CI workflow: on every PR touching `experiments/`, `core/application/`, `infrastructure/workers/`, or `config/`, run `uv run pytest tests/experiments/ tests/integration/test_flat_mode_baseline.py`.
- CI workflow: on every commit to `develop`, run a tiny smoke matrix (1 cell × 1 task × 1 replicate) to catch regressions before they hit a real study.

---

## 3. Open semantic questions (must resolve to unblock specific PRs)

| Q | Question | Blocks | Owner | Status |
|---|---|---|---|---|
| Q1 | What do `naive` / `cybersec` / `full` actually differentiate (B1/B2/B3)? | PR 5 | User (with 30-min code investigation by me) | OPEN — investigation pending |
| Q2 | Is `workspace: raw` (cross-lab comparability) needed? | PR 4 | User decision | OPEN — default is "no", drop unless externally publishing |
| Q3 | Default value of `manifest.replicates` for the study | PR 6 (smoke run) | User decision | Default 1; raise to 3 if statistical claims are intended |
| RESOLVED | `mode: flat` is a new knob (not `topology.max_depth: 0`) | PR 4 | Verified via code reading | DONE |

---

## 4. Risk register (top 5)

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Q1 stays unresolved → PR 5 ships placeholder deltas → study is still meaningless | Medium | High | Block PR 5 on a written delta declaration in the architecture doc |
| 2 | PR 3 strict-mode rejects fields used by some path I didn't trace | Medium | Medium | Run resolver over every committed YAML in CI before merge; iterate |
| 3 | `_execute_flat` produces an event store shape that downstream consumers reject | Low | High | Integration test against collect.py + dashboard load before merge |
| 4 | Parallel matrix exceeds Docker daemon limits and cascades failures | Medium | Medium | Cap default `--parallel` at 1; document operator guidance |
| 5 | Legacy runs in `runs/` and new runs in `output/` produce conflicting lockfile entries | Low | Medium | Hard-error on duplicate run_id across roots |

---

## 5. Verification matrix (after each PR, what must remain true)

| Invariant | PR 1 | PR 2 | PR 3 | PR 4 | PR 5 | PR 6 |
|---|---|---|---|---|---|---|
| `report.md` byte-identical to baseline | ✓ | ✓ | ✓ | — | — | — |
| `report.md` numerically equivalent (rounding tolerance) | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| Existing studies still load | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `Settings.from_yaml(config/config.yaml)` succeeds | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Architecture boundary checks pass | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Matrix driver works end-to-end | — | — | — | — | — | ✓ |
| `diff(B1, B2)` is non-trivial | — | — | — | — | ✓ | ✓ |
| Pre-commit hooks reject malformed manifests | — | — | — | — | — | ✓ |

---

## 6. Suggested execution order (calendar view)

| Day | Activity | Output |
|---|---|---|
| 0 | Investigate Q1 (research, no code) | Concrete B-cell delta proposal merged into architecture doc §3 |
| 1–2 | PR 1 | Inputs/outputs split; legacy data migrated |
| 3 | PR 2 | Groups + runners + validator |
| 4–5 | PR 3 | Overlay resolver + new schema |
| 6–8 | PR 4 | Invariant layer + flat mode + ClaudeCodeWorker |
| 9 | PR 5 | Real B-cell deltas; legacy baselines deleted |
| 10–11 | PR 6 | Matrix driver + provenance + pre-commit hooks |
| 12 | Smoke matrix on the actual study | Validates the whole stack |

A single engineer working sequentially: ~12 days of focused work. Two
engineers can parallelize PR 3 (schema) and PR 1 (input/output split) since
they touch disjoint files.

---

## 7. Out-of-scope follow-ups

- Consolidate `runs/` and `output/` into one canonical root.
- Migrate `runs/_legacy/` into a versioned archive (S3, blob storage) and update lockfile references.
- Add a `--dry-run` to `run_matrix.py` that prints jobs without executing.
- Add per-cell concurrency override (`cell.concurrency: { max_workers: 4 }` in manifest) for cells that tolerate parallelism while others don't.
- Comparison reports across studies (cross-study aggregation).

---

## Appendix A — File inventory of what gets created vs modified vs deleted

### Created
```
config/overlay.py
core/application/run_invariants.py
core/ports/worker_port.py                              # if not present
infrastructure/workers/claude_code_worker.py
infrastructure/snapshot.py
experiments/shared/groups.yaml
experiments/shared/runners/__init__.py
experiments/shared/runners/aris.py
experiments/shared/scripts/validate_manifest.py
experiments/shared/scripts/run_matrix.py
experiments/shared/scripts/audit_dead_fields.py        # one-shot, retained
experiments/<study>/reports/enrollment.lock.yaml
experiments/<study>/reports/matrix-summary.md
tests/experiments/test_invariants.py
tests/integration/test_flat_mode_baseline.py
config/tests/test_overlay.py
experiments/shared/tests/test_collect_enrollment.py
experiments/shared/tests/test_validate_manifest.py
experiments/shared/tests/test_runners_registry.py
experiments/shared/tests/test_run_matrix.py
```

### Modified
```
config/config.yaml                                  # +explicit defaults for new fields
config/settings.py                                  # +new models, strict mode, overlay loading
core/application/execution_service.py               # +mode-flat short-circuit
core/application/agent_orchestrator.py              # +mode propagation
infrastructure/workers/openhands_worker.py          # conform to refined WorkerPort
presentation/persistence/run_persistence.py         # +uv_lock_sha256, +effective_config_path
experiments/shared/harness.py                       # drop manifest writeback; remove run-baseline
experiments/shared/scripts/register_run.py          # drop _upsert_run_in_study
experiments/shared/scripts/collect.py               # produce enrollment.lock.yaml
experiments/shared/scripts/load_runs.py             # multi-pool walker
experiments/shared/scripts/render_report.py         # read lockfile; display via groups.yaml
experiments/shared/scripts/validate_reports.py      # require lockfile + reject runs: in manifest
experiments/<study>/manifest.yaml                   # rewrite cells; bump schema_version
experiments/<study>/configs/A1-claude-code-subagent.yaml      # → overlay
experiments/<study>/configs/A2-claude-code-nosubagent.yaml    # → overlay
experiments/<study>/configs/B1-ours-naive.yaml                # → overlay + real deltas
experiments/<study>/configs/B2-ours-cybersec.yaml             # → overlay + real deltas
experiments/<study>/configs/B3-ours-full.yaml                 # → overlay + real deltas
.pre-commit-config.yaml                             # +validators
existing tests under config/tests/, experiments/shared/tests/
every existing run_manifest.json                    # one-shot rename attempt → replicate
```

### Deleted
```
experiments/shared/baselines/run_claude_code.py
experiments/shared/baselines/run_claude_code_no_subagent.py
experiments/shared/baselines/__init__.py            # if empty
```

---

## Appendix B — Commands cheat-sheet

```bash
# Validate a study before running anything
uv run python -m experiments.shared.scripts.validate_manifest \
    --study 2026-04-23-initial-secbench

# Resolve and inspect a cell config
uv run python -m config.overlay --resolve \
    experiments/2026-04-23-initial-secbench/configs/B2-ours-cybersec.yaml

# Run the matrix (sequential, all cells, all tasks, default replicates)
uv run python -m experiments.shared.scripts.run_matrix \
    --study 2026-04-23-initial-secbench

# Run a focused subset
uv run python -m experiments.shared.scripts.run_matrix \
    --study 2026-04-23-initial-secbench \
    --cells A1,B2 --tasks gpac.cve-2021-40575 --replicates 3 --parallel 2

# Regenerate report from existing runs (no new execution)
uv run python -m experiments.shared.scripts.collect --study <id>
uv run python -m experiments.shared.scripts.plot_success --study <id>
uv run python -m experiments.shared.scripts.render_report --study <id>
uv run python -m experiments.shared.scripts.validate_reports --study <id>

# Audit for dead config fields
uv run python -m experiments.shared.scripts.audit_dead_fields
```
