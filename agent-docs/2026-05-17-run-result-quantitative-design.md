# Run-Result Quantitative-Metrics Module — Design

**Date:** 2026-05-17
**Status:** Design (post-brainstorm). Awaiting user review before implementation plan.
**Branch:** `analysis/db-event-queries` (target for new package commits).
**Scope:** A1/A2 (`a12-batch-autogen`) only. Extension points are wired for B/C but not populated.

---

## 1. Goal

Provide a single function

```python
async def compute_run_result(
    conn: asyncpg.Connection, run_id: UUID, *, family: str = "A",
) -> RunResult
```

that, given a boss-aggregate `run_id`, returns a fully-typed `RunResult` carrying
quantitative metrics and a placeholder for qualitative analysis (currently `None`).
The metrics surface the data needed to compare A1 (Claude Code CLI with
sub-agent `Task` tool enabled) vs A2 (same, sub-agent disabled), and are
designed so a future B-family / C-family taxonomy can plug in without
rewriting the metric computers.

## 2. Constraints (from `experiments/CLAUDE.md`)

- Authoritative sources for any claim: Postgres `events`, on-disk `results/`
  run artifacts, and `experiments/a12-batch-autogen/` study inputs.
- The new package may live only under `experiments/shared/scripts/` because
  `experiments/shared/` is otherwise off-limits and the user has explicitly
  authorised the analysis package alongside the existing `db/` package.
- Markdown reports under `experiments/<study>/reports/` are NOT consulted.

## 3. Authoritative findings that shape the design

| Finding | Source | Implication |
|---|---|---|
| `claude_code_worker.py:609` drops `tool_name` when emitting `ThoughtCaptured(output_type='tool_use')`. SDK adapter at `claude_sdk_adapter.py:328` does not. | Code read + DB query showing 24,023/24,023 null `tool_name` in `stream='claude_code'`. | Tool identity for A1/A2 must be recovered from `payload.content`; for B (when imported) from the structured field. A unified recovery helper handles both. |
| `format_tool_event` (`tool_formatters.py:38-51`) emits a stable prefix-line format. | Code read. | Prefix dispatch table is 1-to-1 for A-family tools. |
| Hard timeout (`worker.timeout`, 5400s) is enforced once at subprocess launch and IS NOT reset for sub-agent runtime. | `claude_code_worker.py:495` `hard_deadline = start + timeout_seconds`. | Sub-agent runtime counts. |
| A second watchdog at `_DEFAULT_INACTIVITY_TIMEOUT_SECONDS = 600` (`claude_code_worker.py:55`) terminates if no stdout for 600s. | Code read. | Distinguishable from hard timeout only by the `WorkFailed.reason` text. |
| `LimitEnforced` is never emitted on timeout — only on topology limits (`depth`, `total_agents`). | `agent_orchestrator.py:736, 751`. | Failure-mode classifier reads `WorkFailed.reason` and the worker `exit_status`. |
| 0 / 329 runs in the DB invoked the `Task` sub-agent tool. | DB regex `^Tool: Task(\n|$)` returns 0. | Subagent-spawn count is a defensible metric whose answer for A1/A2 is empirically zero; design must still implement it correctly. |
| 0 / 329 runs invoked `WebSearch` or `WebFetch`. | DB regex returns 0. | Denylist holds at the data layer. Caveat: cannot distinguish "model never tried" from "CLI silently dropped the attempt." |
| B1 export at `~/b1_export/` has `tool_name` populated on 28,819 / 28,819 tool_use events; uses `stream='claude_sdk'`. | CSV scan. | Confirms recovery helper short-circuits when field is populated. |

## 4. Architecture

### 4.1 Folder layout

```
experiments/shared/scripts/analysis/
├── __init__.py              # public exports
├── run_result.py            # compute_run_result composer
├── models.py                # RunResult, QuantitativeMetrics, sub-metric dataclasses
├── errors.py                # UnknownFamilyError, NotABossRunError, UnknownRunError
├── metrics/
│   ├── __init__.py
│   ├── cost.py              # CostMetrics + compute_cost(events)
│   ├── tools.py             # ToolMetrics, ToolCategory, recover_tool_name, classify_tool
│   ├── outcomes.py          # OutcomeMetrics, failure-mode classifier
│   ├── hierarchy.py         # HierarchyMetrics (degenerate for A, ready for B/C)
│   ├── timing.py            # TimingMetrics
│   └── limits.py            # LimitMetrics
├── text/
│   ├── __init__.py
│   ├── prefixes.py          # CONTENT_PREFIX_TO_TOOL + _TOOL_FALLTHROUGH_RE
│   ├── forbidden_web.py     # WebViolation + detect_violations
│   └── bash_classifier.py   # BashSubtype + classify_bash_command(cmd)
└── tests/
    ├── __init__.py
    ├── factories.py         # synthetic EventRow builders
    ├── test_cost.py
    ├── test_tools.py
    ├── test_outcomes.py
    ├── test_hierarchy.py
    ├── test_timing.py
    ├── test_limits.py
    ├── test_prefixes.py
    ├── test_forbidden_web.py
    ├── test_bash_classifier.py
    ├── test_run_result.py            # composer unit test (mock metric outputs)
    └── test_run_result_real_db.py    # integration, gated on POSTGRES_PASSWORD
```

### 4.2 Dependency direction

```
run_result.py
    → metrics/* (cost, tools, outcomes, hierarchy, timing, limits)
    → models.py, errors.py
    → experiments.shared.scripts.db (EventRow, open_connection, fetch_run_events)

metrics/* → models.py, text/* (where relevant), experiments.shared.scripts.db.EventRow
text/*    → (stdlib only, no DB)
```

`run_result.py` is the ONLY file that touches the DB-fetcher functions. All
metric computers are pure: `(events: Sequence[EventRow]) -> XMetrics`. This
makes them trivially mockable in tests.

## 5. Models

```python
# models.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID
    from datetime import datetime


@dataclass(frozen=True)
class CostMetrics:
    total_llm_cost_usd: float
    total_worker_cost_usd: float
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    llm_call_count: int                                # count of TokensConsumed
    cost_by_model: dict[str, float]                    # model -> sum(cost_usd)
    cost_by_operation: dict[str, float]                # "complexity_evaluation"/"task_decomposition"/"worker_execution"


@dataclass(frozen=True)
class ToolMetrics:
    total_tool_calls: int
    by_tool_name: dict[str, int]
    by_category: dict[str, int]                        # ToolCategory.value → count
    bash_subtypes: dict[str, int]                      # BashSubtype.value → count
    subagent_spawn_count: int                          # exact "Task" tool count
    forbidden_web_attempts: int                        # direct + bash; sum
    forbidden_web_breakdown: dict[str, int]            # {"direct_tool": N, "bash_command": M}
    forbidden_web_violations: tuple[WebViolation, ...] # evidence list with captured text


@dataclass(frozen=True)
class WebViolation:
    via: str                       # "direct_tool" | "bash_command"
    tool_or_cmd: str               # "WebFetch" | the raw shell command
    evidence_snippet: str          # raw text from the event, capped to 500 chars
    aggregate_id: "UUID"
    sequence_number: int


@dataclass(frozen=True)
class OutcomeMetrics:
    run_status: str                                    # "completed" | "failed" | "timed_out" | "in_progress"
    has_run_completed: bool
    has_work_completed: bool
    has_work_failed: bool
    work_completed_count: int
    work_failed_count: int
    verification_passed_count: int
    verification_failed_count: int
    verification_pass_rate: float | None               # None if no verification events
    verification_failed_stages: dict[str, int]
    retries: int
    redecompositions: int
    decisions_infeasible: int
    failure_mode: str | None                           # see §8
    failure_reason: str | None                         # raw WorkFailed.reason for the boss aggregate, if any
    worker_exit_status: str | None                     # if surfaced in event.metadata, else None


@dataclass(frozen=True)
class HierarchyMetrics:
    num_agents: int                                    # AgentCreated count
    max_depth: int                                     # 0 for flat-mode A1/A2
    max_fanout: int
    agents_by_role: dict[str, int]                     # "boss"/"manager"/"worker" → count
    children_spawned: int
    children_completed: int
    children_failed: int


@dataclass(frozen=True)
class TimingMetrics:
    run_started_at: "datetime | None"
    run_completed_at: "datetime | None"
    wall_clock_seconds: float | None
    run_duration_seconds: float | None                 # from RunCompleted.duration_seconds
    agent_execution_total_seconds: float               # sum of AgentExecutionFinished.duration_seconds
    operation_total_seconds: float                     # sum of OperationFinished.duration_seconds
    operation_seconds_by_type: dict[str, float]


@dataclass(frozen=True)
class LimitMetrics:
    total_enforcements: int
    by_limit_type: dict[str, int]                      # "depth"/"total_agents"/...


@dataclass(frozen=True)
class QuantitativeMetrics:
    run_id: "UUID"
    family: str                                        # "A" | "B" | "C"
    event_count: int
    aggregate_count: int
    cost: CostMetrics
    tools: ToolMetrics
    outcomes: OutcomeMetrics
    hierarchy: HierarchyMetrics
    timing: TimingMetrics
    limits: LimitMetrics


@dataclass(frozen=True)
class RunResult:
    run_id: "UUID"
    quantitative: QuantitativeMetrics
    qualitative: None = None                           # reserved; future QualitativeAnalysis
```

All metric dataclasses are `frozen=True` per repo convention.

## 6. Cell extensibility — `TOOL_TAXONOMY`

```python
# metrics/tools.py
import enum
from typing import Final

class ToolCategory(str, enum.Enum):
    FILE_READ      = "file_read"
    FILE_WRITE     = "file_write"
    FILE_EDIT      = "file_edit"
    SEARCH         = "search"
    SHELL          = "shell"
    TASK_MGMT      = "task_mgmt"
    SUBAGENT_SPAWN = "subagent_spawn"
    WEB_FORBIDDEN  = "web_forbidden"
    MCP            = "mcp"
    OTHER          = "other"


# Per-family taxonomy. Each family declares which raw tool_name values
# belong to which semantic category. The same SEMANTIC CONCEPT (e.g.
# "file_edit") must map to whichever raw names that family's runner emits.
TOOL_TAXONOMY: Final[dict[str, dict[ToolCategory, frozenset[str]]]] = {
    "A": {
        ToolCategory.FILE_READ:       frozenset({"Read"}),
        ToolCategory.FILE_WRITE:      frozenset({"Write"}),
        ToolCategory.FILE_EDIT:       frozenset({"Edit", "MultiEdit"}),
        ToolCategory.SEARCH:          frozenset({"Glob", "Grep", "ToolSearch"}),
        ToolCategory.SHELL:           frozenset({"Bash"}),
        ToolCategory.TASK_MGMT:       frozenset({
            "TaskCreate", "TaskUpdate", "TaskList",
            "TaskGet", "TaskStop", "TaskOutput", "TodoWrite",
        }),
        ToolCategory.SUBAGENT_SPAWN:  frozenset({"Task"}),
        ToolCategory.WEB_FORBIDDEN:   frozenset({"WebFetch", "WebSearch"}),
        # MCP via name-prefix predicate, see classify_tool
    },
    # "B": NOT_YET_DEFINED — populate when B-family analysis lands
    # "C": NOT_YET_DEFINED — populate when C-family analysis lands
}


def classify_tool(family: str, tool_name: str) -> ToolCategory:
    table = TOOL_TAXONOMY.get(family)
    if table is None:
        raise UnknownFamilyError(family)
    if tool_name.startswith("mcp__") or tool_name == "security_tools":
        return ToolCategory.MCP
    for category, names in table.items():
        if tool_name in names:
            return category
    return ToolCategory.OTHER
```

User instruction enforced here: **calls for `family="B"` or `family="C"`
raise `UnknownFamilyError` until the user explicitly populates the
table.** No silent guessing.

## 7. Tool-name recovery — `recover_tool_name`

```python
# text/prefixes.py
import re
from typing import Final

_KNOWN_PREFIX_TO_TOOL: Final[dict[str, str]] = {
    "Running: ":           "Bash",
    "Reading: ":           "Read",
    "Writing: ":           "Write",
    "Editing: ":           "Edit",
    "Searching files: ":   "Glob",
    "Searching content: ": "Grep",
}
_TOOL_FALLTHROUGH_RE: Final[re.Pattern[str]] = re.compile(r"^Tool: (\S+)")


def recover_tool_name(content: str, tool_name_field: str | None) -> str:
    """Return the tool name for a tool_use ThoughtCaptured event.

    Precedence:
      1. Structured `tool_name_field` if non-empty (B-family / post-bugfix A).
      2. Content prefix table (A1/A2 historic data).
      3. Fallback "unknown" — always counted, never crashes.
    """
    if tool_name_field:
        return tool_name_field
    first_line = content.split("\n", 1)[0]
    for prefix, name in _KNOWN_PREFIX_TO_TOOL.items():
        if first_line.startswith(prefix):
            return name
    m = _TOOL_FALLTHROUGH_RE.match(first_line)
    if m:
        return m.group(1)
    return "unknown"
```

Dispatch table is exhaustive for A-family because `format_tool_event`
exposes only Claude Code's six registered tools plus the `Tool: <name>`
fallthrough. ADK tools (`read_file` etc.) share two prefixes
(`Running: `, `Reading: `) but are absent from A-family runners.

## 8. Failure-mode classifier — `OutcomeMetrics.failure_mode`

Source signals, in order of priority:

1. `WorkerResult.exit_status` if surfaced in the boss aggregate's
   metadata (probed at implementation time; spec falls back gracefully).
2. `WorkFailed.reason` text on the boss aggregate.

Output values: `"hard_timeout" | "inactivity_timeout" | "provider_failure" | "work_error" | None`.

```python
# metrics/outcomes.py
import re
from typing import Final

_RE_HARD       = re.compile(r"Timed out after \d+s$")
_RE_INACTIVITY = re.compile(r"Timed out after \d+s without Claude output")
_RE_PROVIDER   = re.compile(
    r"\b(api|provider|429|rate.?limit|quota|overload|"
    r"anthropic|openai|litellm)\b", re.IGNORECASE,
)


def classify_failure(
    reason: str | None,
    exit_status: str | None,
) -> str | None:
    if exit_status == "timeout":
        return _classify_timeout_kind(reason)
    if reason:
        if _RE_HARD.search(reason):       return "hard_timeout"
        if _RE_INACTIVITY.search(reason): return "inactivity_timeout"
        if _RE_PROVIDER.search(reason):   return "provider_failure"
        return "work_error"
    if exit_status not in (None, "success", "completed"):
        return "work_error"
    return None


def _classify_timeout_kind(reason: str | None) -> str:
    if reason and _RE_INACTIVITY.search(reason):
        return "inactivity_timeout"
    return "hard_timeout"
```

Reason patterns are derived from `claude_code_worker.py:500, 506-508`:

| Source line | Reason string format |
|---|---|
| `:500` | `f"Timed out after {timeout_seconds}s"` |
| `:506-508` | `f"Timed out after {inactivity_timeout_seconds}s without Claude output"` |

Both ends of the string are owned by this repo, so the regex contract is
stable.

## 9. Forbidden-web detection — both direct AND indirect (user choice "c")

```python
# text/forbidden_web.py
import json, re
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Final

BASH_WEB_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"curl|wget|nc(?:at)?\s+-|"
    r"python3?\s+-c\s+['\"].*?(?:requests|urllib|httpx)|"
    r"socat\s+|"
    r"openssl\s+s_client|"
    r"ftp\s+|sftp\s+"
    r")"
    r"|https?://",
    re.IGNORECASE,
)
_DIRECT_WEB_TOOL_NAMES: Final[frozenset[str]] = frozenset({"WebFetch", "WebSearch"})
_INPUT_PREFIX = "\nInput: "
_EVIDENCE_CAP = 500


@dataclass(frozen=True)
class WebViolation:
    via: str                       # "direct_tool" | "bash_command"
    tool_or_cmd: str
    evidence_snippet: str
    aggregate_id: "UUID"
    sequence_number: int


def detect_violations(
    events: Sequence["EventRow"],
) -> list[WebViolation]:
    out: list[WebViolation] = []
    for e in events:
        if e.event_type != "ThoughtCaptured":            continue
        payload = e.payload
        if payload.get("output_type") != "tool_use":     continue
        content = payload.get("content") or ""
        tool_name = recover_tool_name(content, payload.get("tool_name"))
        if tool_name in _DIRECT_WEB_TOOL_NAMES:
            out.append(WebViolation(
                via="direct_tool",
                tool_or_cmd=tool_name,
                evidence_snippet=content[:_EVIDENCE_CAP],
                aggregate_id=e.aggregate_id,
                sequence_number=e.sequence_number,
            ))
            continue
        if tool_name == "Bash":
            cmd = _extract_bash_command(content)
            if cmd and BASH_WEB_RE.search(cmd):
                out.append(WebViolation(
                    via="bash_command",
                    tool_or_cmd=cmd[:_EVIDENCE_CAP],
                    evidence_snippet=cmd[:_EVIDENCE_CAP],
                    aggregate_id=e.aggregate_id,
                    sequence_number=e.sequence_number,
                ))
    return out


def _extract_bash_command(content: str) -> str | None:
    idx = content.find(_INPUT_PREFIX)
    if idx < 0: return None
    try:
        obj = json.loads(content[idx + len(_INPUT_PREFIX):])
    except (json.JSONDecodeError, TypeError):
        return None
    cmd = obj.get("command") if isinstance(obj, dict) else None
    return cmd if isinstance(cmd, str) else None
```

`ToolMetrics.forbidden_web_attempts` is `len(violations)`;
`forbidden_web_breakdown` counts by `via`; `forbidden_web_violations`
holds the evidence list. Per user instruction "filter the texts (i.e.
wget command itself from event)" — the raw command text is captured in
`tool_or_cmd` and `evidence_snippet`.

## 10. Bash subtype classifier — `text/bash_classifier.py`

Regex-based dispatch over the Bash command string extracted from
`Input.command`. Same `_extract_bash_command` helper as §9.

| Subtype | Patterns (rough) |
|---|---|
| `web_via_shell` | covered by `BASH_WEB_RE` |
| `recon` | `nmap`, `masscan`, `gobuster`, `dirb`, `nikto`, `whatweb`, `hydra` |
| `security_scan` | `bandit`, `semgrep`, `codeql`, `trivy`, `grype`, `osv-scanner`, `safety` |
| `exploit` | `sqlmap`, `metasploit`, `msfconsole`, `pwntools` |
| `build` | `gcc`, `clang`, `make`, `cmake`, `cargo build`, `go build`, `meson`, `ninja` |
| `test_exec` | `pytest`, `gtest`, `./fuzz`, `afl-fuzz`, `libfuzzer` |
| `git` | `^\s*git\s` |
| `other_shell` | fallthrough |

All patterns are stored as a single ordered list of `(BashSubtype,
re.Pattern)` tuples at the top of `text/bash_classifier.py` so the
priority is unambiguous and revisable in one place. The `web_via_shell`
entry imports `BASH_WEB_RE` directly from `text/forbidden_web.py`,
making the forbidden-web detector the single source of truth for the
web-egress alternation. This eliminates the divergence risk that would
otherwise allow a bash command (e.g. `python3 -c '… requests …'`) to be
flagged as a forbidden-web violation but misclassified as
`other_shell` by the subtype counter. Subtypes that overlap (e.g. a
Bash command runs `curl ... | sh`) increment **only one** counter —
first match in the ordered list wins. Order: `web_via_shell` > `recon`
> `exploit` > `security_scan` > `build` > `test_exec` > `git` >
`other_shell`.

## 11. Run-id semantics & errors

`compute_run_result(conn, run_id)` accepts the BOSS aggregate id. Any
other aggregate raises `NotABossRunError`. The contract is enforced by
scanning `fetch_run_events` for a `RunStarted` event on the same
aggregate as `run_id`:

```python
events = await fetch_run_events(conn, run_id)
if not events:
    raise UnknownRunError(f"no events for {run_id}")
boss_run_started = next(
    (e for e in events
     if e.aggregate_id == run_id and e.event_type == "RunStarted"),
    None,
)
if boss_run_started is None:
    raise NotABossRunError(
        run_id=run_id,
        first_event_type=events[0].event_type,
        first_aggregate_id=events[0].aggregate_id,
    )
```

Real boss aggregates emit `AgentCreated` (seq=1), `TaskAssigned` (seq=2),
`RunStarted` (seq=3+) in that order, and `fetch_run_events` returns
events ordered by `(occurred_at, sequence_number)`. Consequently the
*first* event on a real boss aggregate is **never** `RunStarted`. The
boss predicate is therefore "this aggregate emitted a `RunStarted`
somewhere in its event stream", not "the first event is `RunStarted`".

`errors.py`:
```python
class AnalysisError(Exception): ...
class UnknownFamilyError(AnalysisError): ...
class NotABossRunError(AnalysisError): ...
class UnknownRunError(AnalysisError): ...
```

## 12. Composer — `run_result.py`

```python
async def compute_run_result(
    conn: asyncpg.Connection,
    run_id: UUID,
    *,
    family: str = "A",
) -> RunResult:
    events = await fetch_run_events(conn, run_id)
    if not events:
        raise UnknownRunError(run_id)
    boss_run_started = next(
        (e for e in events
         if e.aggregate_id == run_id and e.event_type == "RunStarted"),
        None,
    )
    if boss_run_started is None:
        raise NotABossRunError(...)

    qm = QuantitativeMetrics(
        run_id=run_id,
        family=family,
        event_count=len(events),
        aggregate_count=len({e.aggregate_id for e in events}),
        cost=compute_cost(events),
        tools=compute_tools(events, family=family),
        outcomes=compute_outcomes(events, run_id=run_id),
        hierarchy=compute_hierarchy(events, run_id=run_id),
        timing=compute_timing(events, run_id=run_id),
        limits=compute_limits(events),
    )
    return RunResult(run_id=run_id, quantitative=qm)
```

## 13. Testing strategy

### 13.1 Synthetic factories — `tests/factories.py`

Helpers to build `EventRow` objects matching every event type the metric
computers consume, with sensible defaults. Helpers also produce content
strings shaped like `format_tool_event` output, so prefix-recovery is
exercised against realistic inputs.

```python
def tool_use_event(
    aggregate_id: UUID, seq: int, tool_name: str, tool_input: dict,
    *, set_structured_field: bool = False,
) -> EventRow: ...
# set_structured_field=False mimics A1/A2; True mimics B1.
```

### 13.2 Unit tests

One test file per metric module. Each tests:
- Happy path with synthetic events
- Empty-input behaviour (no events of the relevant type)
- Edge cases specific to that metric (e.g. tied timestamps, multiple aggregates)

### 13.3 Forbidden-web tests

Coverage matrix:
- Direct `Tool: WebFetch` event → 1 violation, `via="direct_tool"`
- Direct `Tool: WebSearch` event → 1 violation, `via="direct_tool"`
- Bash command `curl https://x` → 1 violation, `via="bash_command"`, evidence captures the URL
- Bash command `wget -O /tmp/foo http://y` → 1 violation
- Bash command `nc -lvnp 4444` → 1 violation (matches `nc(?:at)?\s+-`)
- Bash command `python3 -c "import requests; requests.get('https://z')"` → 1 violation
- Bash command `ls -la` → 0 violations (negative case)
- Bash command containing literal `https://` in a `--description` flag — TBD; current regex matches `https?://` anywhere, so this would over-trigger. Design decision: accept the over-trigger as conservative.

### 13.4 Failure-mode tests

- `WorkFailed.reason="Timed out after 5400s"` → `hard_timeout`
- `WorkFailed.reason="Timed out after 600s without Claude output"` → `inactivity_timeout`
- `WorkFailed.reason="LLM provider returned 429"` → `provider_failure`
- `WorkFailed.reason="something else"` → `work_error`
- No `WorkFailed`, `RunCompleted(status="completed")` → `None`

### 13.5 Integration — `tests/test_run_result_real_db.py`

Gated on `POSTGRES_PASSWORD` (matching the `db/` package convention).
Picks a known-good A1 run id from the DB and asserts:
- `family="A"` returns successfully
- `quantitative.event_count > 0`
- `quantitative.tools.subagent_spawn_count >= 0`
- `quantitative.cost.total_llm_cost_usd >= 0`
- `family="B"` raises `UnknownFamilyError`

### 13.6 Contract tests

- Passing a non-boss aggregate id raises `NotABossRunError`.
- Passing a UUID with no events raises `UnknownRunError`.

## 14. Caveats and known gaps

1. **`tool_name` bug at `claude_code_worker.py:609`** — being fixed on a
   separate branch (`fix/claude-code-tool-name-propagation`). Once
   merged + re-projected, A1/A2 events going forward will have the
   structured field populated; historic data continues to be recovered
   via prefix parsing. Both paths are unified by `recover_tool_name`.
2. **Disallowed-tool emission behaviour** — when the model invokes a
   tool in `worker.disallowed_tools` (e.g. `WebFetch`), it is unknown
   whether the Claude Code CLI silently drops the attempt or emits a
   `tool_use` block that we'd then capture. Historic data has 0
   `Tool: WebFetch` rows; this could mean the model never tried or that
   the CLI hides the attempt. The forbidden-web indirect path (Bash
   with URLs) is unaffected. A future smoke test against a real CLI
   invocation can resolve this; not blocking.
3. **Sub-agent attribution is impossible from current schema** — when
   `Task` spawns a sub-agent and the sub-agent makes its own tool calls,
   our parser emits those as additional `ThoughtCaptured` rows on the
   parent aggregate, indistinguishable from the parent's own tool
   calls. The CLI's stream-json carries a `parent_tool_use_id` field but
   our schema has no place for it. `subagent_spawn_count` measures only
   the count of `Task` invocations; attribution of the sub-agent's
   internal tool calls is out of scope.
4. **Sub-agent cost attribution** — same root cause. The CLI's terminal
   `{"type":"result", "usage": …}` line may aggregate sub-agent token
   usage into the parent, or may not. `WorkerCostRecorded` reports the
   total; we cannot break out sub-agent cost. Empirically moot for the
   current corpus (0 Task usage).
5. **`worker.exit_status` provenance** — at implementation time, search
   for whether `WorkerResult.exit_status` is surfaced into the boss
   aggregate's event metadata. If yes, `OutcomeMetrics.worker_exit_status`
   is populated. If no, it stays `None` and the failure-mode classifier
   relies on reason-text only.
6. **Bash regex over-triggers** — `https?://` matches anywhere in the
   command including comments and descriptions. The design accepts this
   as a conservative false-positive bias (better to flag a false
   positive than miss a real cheat).

### Design correction 2026-05-17

The original §12 used `events[0].event_type == "RunStarted"` for boss
detection, which was wrong because the event-store ordering places
`AgentCreated` (seq=1) and `TaskAssigned` (seq=2) before `RunStarted`
(seq=3+) on real boss aggregates. Every real boss therefore would have
been mis-classified as non-boss and rejected with `NotABossRunError`.
Mocked unit tests masked the bug by putting `RunStarted` at index 0 of
their synthetic event lists; the integration test against a live A1
boss surfaced it.

Corrected to scan for a `RunStarted` event on the same aggregate as
`run_id` (see §11 and §12). The happy-path unit test was rewritten to
mirror the realistic ordering — `AgentCreated(seq=1) →
TaskAssigned(seq=2) → RunStarted(seq=3) → … → RunCompleted(seq=N)` — so
the bug cannot regress. An additional regression test exercises a
multi-event stream where `RunStarted` is present but on a CHILD
aggregate; the composer correctly rejects it because the boss predicate
filters on `aggregate_id == run_id`.

In the same pass, `_BASH_WEB_RE` was promoted to the public
`BASH_WEB_RE` in `text/forbidden_web.py` and reused as the
`WEB_VIA_SHELL` pattern in `text/bash_classifier.py`. Previously the
two regexes diverged: `forbidden_web.py` already covered
`python3 -c '… (requests|urllib|httpx)'` but `bash_classifier.py` did
not, so the same Bash command was flagged as a forbidden-web
violation yet bucketed as `other_shell` in the subtype counter. The
two now share a single source of truth.

## 15. Extension contract for B/C

When the user is ready to extend to B-family, the following changes:

1. **`experiments/CLAUDE.md` rule 3** must be updated to include the B-family experiment folder (per the scope-drift guard).
2. **`TOOL_TAXONOMY["B"]`** is populated with raw tool names mapped to
   each `ToolCategory`. The mapping MUST be confirmed with the user; the
   metrics module must NOT guess.
3. No metric computer code changes — they consume `EventRow` regardless
   of family, with `family` used only in `metrics/tools.py::classify_tool`.
4. The integration test gains a B-family run id and the same assertion
   shape.

C-family follows the same contract.

## 16. Out of scope (this design)

- `QualitativeAnalysis` — `RunResult.qualitative` stays `None` until a
  separate design.
- Report rendering (CSV/Markdown). The module returns typed Python
  objects; rendering is a separate concern.
- Cross-run comparison (A1 vs A2 paired stats). Operates over a single
  run; pairwise comparison lives one layer up.
- Sub-agent attribution / sub-agent cost — see Caveats §14.3-4.
- Fixing the `tool_name` bug — on a separate branch (see Caveats §14.1).

## 17. Acceptance criteria

The implementation is complete when:

- `compute_run_result(conn, boss_id, family="A")` returns a fully
  populated `RunResult` for any of the 109 A1/A2 runs in the DB without
  raising.
- `compute_run_result(conn, non_boss_id, family="A")` raises
  `NotABossRunError`.
- `compute_run_result(conn, boss_id, family="B")` raises
  `UnknownFamilyError`.
- All unit tests pass under `uv run pytest experiments/shared/scripts/analysis/tests`.
- Integration test passes when `POSTGRES_PASSWORD` is set.
- `ruff check experiments/shared/scripts/analysis` is clean.
- `pyright experiments/shared/scripts/analysis` has no new errors.

---

End of design.
