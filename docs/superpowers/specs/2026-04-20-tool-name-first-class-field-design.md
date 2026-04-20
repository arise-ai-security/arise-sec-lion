# Design: Thread `tool_name` as First-Class Field on `ThoughtCaptured`

**Date:** 2026-04-20
**Status:** Approved
**Branch:** `experiment/tree-vs-flat-agent-orchestration`

## Problem

The Claude Agent SDK provides the exact tool name (`Read`, `Bash`, `Write`, `Edit`, `Grep`, `Glob`) on every tool call via `PostToolUseHookInput["tool_name"]` and `ToolUseBlock.name`. The adapter captures this value but bakes it into a human-readable `content` string (e.g. `"Running: ls -la"`). The raw tool name never reaches the `ThoughtCaptured` domain event.

Downstream, the projection layer (`tree_projection.py`) regex-reconstructs the tool name from `content` using a brittle verb-to-tool map. This is lossy: tools outside the map produce `"Unknown"`, and any formatter change silently breaks analysis.

## Decision

Trust the SDK. Capture `tool_name` at source, persist it as a first-class field on `ThoughtCaptured`, and read it directly in the projection layer. Delete the regex reconstruction.

## Design

### 1. `core/domain/events/events.py` — `ThoughtCaptured`

Add one field:

```python
tool_name: str | None = None
```

Optional (`None`) for backward compatibility with non-tool events (`thinking`, `output`, `progress`) and historical events that predate this change.

### 2. `infrastructure/adapters/worker/shared/event_sequencer.py` — `EventSequencer.thought()`

Add `tool_name: str | None = None` parameter. Pass through to `ThoughtCaptured` constructor. No sanitization needed (tool names are short ASCII identifiers).

### 3. `infrastructure/adapters/worker/claude_sdk_adapter.py`

**Queue tuple type** grows from 4 to 5 elements:

```
(content, output_type, tool_use_id, tool_input_json, tool_name)
```

**Hook path (runtime):** `capture_tool_use` already reads `input_data["tool_name"]`. Append it as the 5th queue element. `_drain_queue` unpacks and passes `tool_name=` to `sequencer.thought()`.

**Block path (helper/test only):** `_process_block` for `ToolUseBlock` adds `"tool_name": block.name` to the metadata dict. At runtime `_process_message` skips `ToolUseBlock` (the hook path owns emission), but `_process_block` is tested directly for correctness and for any future hook-free replay path.

### 4. `experiments/tree_projection.py`

`_convert_event` reads `getattr(ev, "tool_name", None)` from the persisted event and passes it to `ToolUsePayload.tool_name`. For historical events where the field is `None`, emit `"Unknown"`.

Delete `_extract_tool_name()` entirely.

### 5. Tests — `infrastructure/tests/test_claude_sdk_adapter.py`

| Test | What it proves |
|------|---------------|
| Hook path e2e: PostToolUse -> queue -> `_drain_queue` -> `ThoughtCaptured` | `tool_name` from SDK survives into the domain event |
| Block path: `_process_block(ToolUseBlock(name="Read"))` -> metadata -> sequencer | Fallback path also preserves `tool_name` |
| Sequencer unit: `thought(tool_name="Grep")` vs `thought()` | Field is `"Grep"` when provided, `None` when omitted |
| Projection: `ThoughtCaptured(tool_name="Bash")` -> `ToolUsePayload` | Normalized payload uses persisted field, not regex |

## Files Changed

| File | Layer | Change |
|------|-------|--------|
| `core/domain/events/events.py` | core | Add `tool_name: str \| None = None` |
| `infrastructure/adapters/worker/shared/event_sequencer.py` | infra | Add `tool_name` param to `thought()` |
| `infrastructure/adapters/worker/claude_sdk_adapter.py` | infra | Thread through hook + block paths |
| `experiments/tree_projection.py` | experiments | Use `tool_name` directly, delete regex |
| `infrastructure/tests/test_claude_sdk_adapter.py` | tests | 4 new test cases |

## What This Does NOT Change

- `ThoughtCaptured.content` still contains the formatted string (for human readability in logs/UI)
- `tool_input_json` is unchanged (already captured correctly)
- The OpenHands adapter is unaffected (it does not have structured tool names from its SDK)
- No database migration needed (`payload` is JSONB; new key appears automatically)
- No event replay concerns (field is optional with `None` default)
