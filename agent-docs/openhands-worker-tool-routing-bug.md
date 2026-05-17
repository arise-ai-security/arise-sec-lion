# OpenHands Worker Tool-Routing Bug — Commands Run on Host, Not Container

**Date**: 2026-05-11
**Severity**: **High** — silently corrupts results for valgrind, KLEE, gcc, and any benchmark tool that lives only inside the SEC-bench container.
**Status**: Open. Identified during the parallel=20 host-saturation reproduction (see `parallel-20-host-saturation-diagnosis.md`).

---

## TL;DR

The OpenHands worker adapter registers `TerminalTool` with `terminal_type=subprocess`. That subprocess runs on the **host**, in the host's filesystem and PATH, **not** inside the `secbench-worker-*` Docker container. The container holds Valgrind, KLEE, the CVE-specific build of the target binary, KASAN/UBSAN-instrumented libraries, and the `/testcase/` artefacts — none of which are present on the host. The bridge (`./secb-exec` helper) routes commands into the container, but use of that helper is *advisory* — the agent is *prompted* to use it, not forced.

Result: when the agent issues `valgrind ./repro`, it runs against whatever `valgrind` is on the host (often nothing, or a different version, or built against different system libs). For KLEE, it almost certainly fails outright because KLEE depends on the LLVM bitcode and runtime built inside the container.

---

## 1. Direct Evidence (matrix.log from parallel=20 run)

From `/tmp/c1-repro/matrix.log`:

```
Tool: terminal
Result:
ls: /testcase/: No such file or directory

📁 Working directory:
/Users/garfield/PycharmProjects/arise-sec-lion/runs/5318d560-115c-48f3-b186-0e10d48e24d1
🐍 Python interpreter:
/Users/garfield/PycharmProjects/arise-sec-lion/.venv/bin/python
❌ Exit code: 1
```

Three things this proves:

1. `/testcase/` is a **container path** (created by the SEC-bench image at `/testcase/<cve>.sh`); on the host it does not exist.
2. The `📁 Working directory:` is a **host path** (`/Users/garfield/.../runs/<run_id>/`) — the workspace mounted to `workspace.host_root`.
3. The `🐍 Python interpreter:` is the **host's** `.venv/bin/python` — so when the agent runs a Python snippet, it picks up host packages, not container packages.

Same symptom for shell commands:
```
Tool: terminal
Result:
bash: cd: /src/njs: No such file or directory
```

`/src/njs/` exists **inside** the secbench-worker container (the upstream image's source mount). On the host it does not — the host has the *checked-out copy* at `workspace.host_source_dir`, but not at `/src/njs/`.

The agent (Haiku in this case) saw the system prompt instructing it to use `./secb-exec "<cmd>"` for container-side work, but defaulted to plain shell because TerminalTool is the most natural surface.

---

## 2. Why It Happens — Code Walk

### 2.1 Worker tools registered in `OpenHandsAdapter`

`infrastructure/adapters/worker/openhands_adapter.py:233-294`:

```python
from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool
...
tools = []
if self._tool_allowed("Bash", "Terminal", "execute_command", "shell_execute"):
    tools.append(
        Tool(
            name=TerminalTool.name,
            params={"terminal_type": "subprocess"},   # ← runs on host
        )
    )
if self._file_editor_allowed():
    tools.append(Tool(name=FileEditorTool.name))      # ← reads/writes host paths

mcp_config = to_openhands_mcp_config(mcp_servers) if mcp_servers else {}
agent = Agent(llm=llm, tools=tools, mcp_config=mcp_config)
return Conversation(
    agent=agent,
    workspace=working_dir,    # ← host_root, host path
    max_iteration_per_run=self.max_iterations_per_run,
)
```

`terminal_type=subprocess` selects `openhands.tools.terminal.terminal.subprocess_terminal.SubprocessTerminal`, which `Popen`s a shell on the **local host** rooted at `workspace`.

### 2.2 The container bridge is a separate, advisory channel

`plugins/security/docker_runtime.py:225-243` (`_write_exec_helper`) writes a script `secb-exec` into `workspace.host_root` that, when invoked, does:

```bash
docker exec -i -w <container_working_directory> <container_id> bash -lc "$*"
```

This is the *only* path that runs inside the container. The MCP security-tools server (`plugins/security/security_tools_server.py`) shells out to `secb-exec` for its tool calls — that's why MCP-backed tools (e.g. `search_codebase`, `get_symbols_overview`) correctly run in-container.

### 2.3 The agent is told via prompt — not enforced

`infrastructure/adapters/worker/shared/container_session.py:67-115` (`apply_task_prefix`) prepends the task description with:

```
⚠️ ABSOLUTE RULE — TWO SEPARATE FILESYSTEMS:
You are running on a HOST machine. Source code and testcase files are mirrored
between host and a Docker container. The file editor tool operates on HOST paths.
Container paths like `/src/...` and `/testcase/...` DO NOT EXIST on the host and
WILL ERROR.

Shell commands — ALWAYS use the helper script `./secb-exec` for container commands:
- ALL build/test/runtime shell commands MUST use `./secb-exec "<command>"`.
- Do NOT run them directly — they will fail because container paths do not exist on this host.
- Example: `./secb-exec "cd /src/<project> && make"` (NOT `cd /src/<project> && make`)
```

This is the only mechanism enforcing the routing. **Whenever the agent forgets**, the command runs on host. Under cognitive load (deep recon prompts, many tool calls, long context), Haiku and even Sonnet routinely forget — we observed it dozens of times in this run.

### 2.4 Compare: Claude Code SDK adapter does NOT have this bug

`infrastructure/adapters/worker/claude_sdk_adapter.py:147` uses `container_session.translate_tool_input(tool_name, tool_input)` — a wrapper applied to every tool invocation that:

- Translates `/src/...` → `host_source_dir/...`
- Wraps `Bash` commands with `wrap_shell_command(...)` (which prepends `./secb-exec "..."`)

Same mechanism does not exist for OpenHands.

---

## 3. Specific Impact on Valgrind & KLEE

### 3.1 Valgrind

The `secb-tools:<cve>-patch` image (built by `deployment/build-secbench-tools.sh`) installs Valgrind inside the container, against the same libc/CRT used to compile the target. The host's Valgrind (if installed at all) is:

- A different version (typically Homebrew's 3.x vs. upstream's git)
- Linked against host libc/libsystem, not the container's glibc
- Missing the suppression files baked into the SEC-bench image

If the agent runs `valgrind ./build/openjpeg/bin/opj_decompress poc.j2k`:
- **Best case**: host has no Valgrind → "command not found" → agent thinks the binary lacks the bug, marks the run as no-repro.
- **Common case**: host has Valgrind but loads host's libc → false negatives or false positives — the report does not reflect the in-container binary's actual behavior.
- **In all cases**: the run's outcome no longer corresponds to "did the patch fix the CVE under SEC-bench's tooling?" — the experiment becomes meaningless.

### 3.2 KLEE

KLEE is even more fragile. It requires:
- LLVM bitcode produced by the container's `wllvm`-wrapped build
- A bitcode runtime (`klee-uclibc`) compiled inside the container
- `/testcase/<cve>.sh` and `/src/<project>/build/...` paths to exist exactly where KLEE was configured

Running KLEE on the host will fail before it does any work: it will not find the bitcode at the path the build wrote it to (container path), or it will find a host-built bitcode against host LLVM, which mismatches the symbolic-execution runtime.

### 3.3 gcc / build.sh

Same class of problem. `build.sh` inside the container depends on:
- `CFLAGS` / `LDFLAGS` set in the container's image entrypoint
- `LD_LIBRARY_PATH` pointing to the container's `/src/<project>/build/lib`
- Submodule dependencies under `/src/<project>/...`

Building on host either fails (missing deps) or produces a binary linked against the wrong libraries — which would not reproduce the CVE.

---

## 4. Why the Helper-Script Approach Is Insufficient

The current design treats container routing as an **agent obligation** ("read the system prompt, remember the rule, always prefix with `./secb-exec`"). This works under three assumptions, all of which fail under realistic load:

1. *The agent reads and obeys every system-prompt instruction.* Empirically false even for Sonnet, much more so for Haiku. The 20-parallel run produced multiple host-rooted `ls /testcase/` and `cd /src/...` calls per session.
2. *The cost of a missed routing is bounded* (just an error). For `ls` it's an error — but for `valgrind ./repro` the host may *successfully* run a different version and return a misleading result. **Silent wrong answers are far worse than loud errors.**
3. *The agent can detect that a command should be container-side* without inspecting filesystem state. For commands that touch paths starting with `/src/` or `/testcase/` this is detectable; for commands that should run from `cwd` it's not.

The MCP security-tools path doesn't have this problem because *its tool implementations* (in `security_tools_server.py`) always invoke `secb-exec` — the agent never has the option to bypass.

---

## 5. Proposed Fixes

### Option A — Drop TerminalTool, force shell through MCP (recommended)

Remove the `TerminalTool` registration in `openhands_adapter.py:286-292`. Add a `shell_in_container` tool to the security-tools MCP server (`plugins/security/security_tools_server.py`) that just `secb-exec`s its argument.

**Pros**: agent has no shell tool except the container-bound one. Zero possibility of escape. Mirrors how `search_codebase` etc. already work.
**Cons**: loses OpenHands' interactive PTY features (multi-step shell sessions with shared state). For SEC-bench's use case (compile, run, check output) this is acceptable; for general agentic shell work it would matter.

### Option B — Wrap TerminalTool with a `docker exec` shim

Subclass `TerminalTool` (or pass a custom `terminal_type`) that, before running any command, prepends `docker exec -i -w <cwd> <container_id> bash -lc "..."`. Implementation can sit alongside `to_openhands_mcp_config` in `infrastructure/adapters/worker/shared/`.

**Pros**: Keeps the TerminalTool surface (interactive sessions etc. could still work if you maintain an exec stream); less invasive to the wider SDK contract.
**Cons**: Has to handle cwd translation (`workspace.host_root/...` → `workspace.container_workspace_root/...`), environment passthrough, and Bash quoting. The Claude SDK adapter already solves these in `container_session.translate_tool_input` + `wrap_shell_command` — reuse that.

### Option C — Mount the helper into the agent's PATH

Symlink `./secb-exec` into a location on the host's `PATH` so that commands like `valgrind …` get rewritten by the agent into `secb-exec valgrind …` — no, this still depends on the agent's cooperation. **Rejected**.

### Option D — Disallow ambiguous tools at the policy layer

Already done partially (`_tool_allowed`, `_file_editor_allowed`); but the only way to disable shell entirely is to set `worker.disallowed_tools: ["Bash", "Terminal", "execute_command", "shell_execute"]`. If you do that *without* providing an MCP replacement, the agent has no way to run commands and the cell becomes useless.

**Recommendation: A.** It's the cleanest, mirrors the existing MCP pattern, and removes the entire class of "agent forgot the rule" failures. Implementation effort is small — one new tool in `security_tools_server.py`, deletion of ~8 lines in `openhands_adapter.py`.

---

## 6. Files Touched / Referenced

| Path | Role |
|---|---|
| `infrastructure/adapters/worker/openhands_adapter.py:233-308` | Where TerminalTool + FileEditorTool are registered |
| `infrastructure/adapters/worker/shared/container_session.py:67-130` | `apply_task_prefix` (prompt-based routing) + `translate_tool_input` + `wrap_shell_command` (already used by Claude SDK adapter) |
| `infrastructure/adapters/worker/claude_sdk_adapter.py:147` | Reference implementation of correct routing |
| `plugins/security/docker_runtime.py:225-243` | `_write_exec_helper` — writes `secb-exec` script |
| `plugins/security/plugin.py:201-220` | `prepare_worker_execution` — passes `helper_script` into `task_context` and MCP config |
| `plugins/security/security_tools_server.py` | MCP server that already routes through `secb-exec` correctly |
| `infrastructure/adapters/worker/shared/__init__.py:3` | Exports `ContainerSessionContext` |
| `/tmp/c1-repro/matrix.log` (lines ~52000-54000 and elsewhere) | Captured evidence of `ls: /testcase/` and `cd: /src/njs:` errors during the 20-parallel run |

---

## 7. Recommended Test After Fix

Once fix is in place, re-run the parallel=20 reproduction (see `parallel-20-host-saturation-diagnosis.md §9`) with `--parallel 5` (to isolate the routing fix from the saturation issue) and grep:

```bash
# These should both return ZERO hits after the fix
grep -c "No such file or directory" /tmp/c1-repro-postfix/matrix.log
grep -cE "🐍 Python interpreter: /Users/garfield" /tmp/c1-repro-postfix/matrix.log
```

If both are zero, every shell command routed correctly into the container.
