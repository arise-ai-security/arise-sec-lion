# RESEARCHER Node Implementation

This document describes the RESEARCHER agent role, which performs read-only tool calling for context gathering before transitioning to MANAGER for task decomposition.

## Agent State Machine

```
PENDING ──► ComplexityEvaluated ──┬──► WORKER (simple)
            (ANALYZING)           │
                                  ├──► RESEARCHER (complex + needs_research)
                                  │        │
                                  │        ▼ ResearchStarted
                                  │    IN_PROGRESS
                                  │        │
                                  │        ▼ ResearchCompleted
                                  │        │
                                  │        ▼ RoleTransitioned
                                  │        │
                                  └──► MANAGER (complex) ◄─┘
                                          │
                                          ▼ SubtasksDefined
                                       WAITING
```

**State Transitions:**

| From | Event | To | Description |
|------|-------|-----|-------------|
| PENDING | ComplexityEvaluated (needs_research=true) | RESEARCHER | Agent needs context before decomposition |
| RESEARCHER | ResearchStarted | IN_PROGRESS | Research phase begins |
| RESEARCHER | ResearchCompleted | - | Findings stored |
| RESEARCHER | RoleTransitioned | MANAGER | Transition to task decomposition |

## Module Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CORE LAYER                               │
├─────────────────────────────────────────────────────────────────┤
│  ports/research_port.py                                          │
│  ├─ ResearchPort (Protocol)                                      │
│  ├─ ResearchResult (dataclass)                                   │
│  └─ RESEARCH_TOOL_NAMES ◄── Contract between core & infra        │
│                                                                  │
│  application/pipeline/steps/research.py                          │
│  ├─ StartResearch                                                │
│  ├─ RunResearchSession                                           │
│  ├─ ApplyResearchResult                                          │
│  └─ EmitResearchTokensConsumed                                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ implements
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     INFRASTRUCTURE LAYER                         │
├─────────────────────────────────────────────────────────────────┤
│  adapters/research/                                              │
│  ├─ adapter.py           ◄── LLM orchestration only (SRP)        │
│  ├─ tool_registry.py     ◄── Tool definitions + execution        │
│  └─ tools/                                                       │
│      └─ web_fetch.py     ◄── Custom tool (not in OpenHands)      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ uses
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     EXTERNAL DEPENDENCIES                        │
├─────────────────────────────────────────────────────────────────┤
│  openhands-tools                                                 │
│  ├─ GrepExecutor       ──► grep_search                           │
│  ├─ GlobExecutor       ──► list_files                            │
│  └─ FileEditorExecutor ──► file_read                             │
│                                                                  │
│  prompts/core/roles/researcher.j2  ◄── Generic prompt template   │
└─────────────────────────────────────────────────────────────────┘
```

## File Structure

```
core/
├── ports/
│   └── research_port.py          # Port protocol + tool name contract
├── domain/
│   ├── values/enums.py           # AgentRole.RESEARCHER
│   ├── events/events.py          # ResearchStarted, ResearchCompleted, RoleTransitioned
│   └── aggregates/agent_session.py  # start_research(), transition_to_manager()
└── application/
    └── pipeline/steps/research.py   # Pipeline steps

infrastructure/
└── adapters/
    └── research/
        ├── __init__.py           # Exports
        ├── adapter.py            # LiteLLMResearchAdapter
        ├── tool_registry.py      # ResearchToolRegistry
        └── tools/
            ├── __init__.py
            └── web_fetch.py      # WebFetchExecutor

prompts/
└── core/
    └── roles/
        └── researcher.j2         # Role prompt template
```

## Available Research Tools

| Tool | Provider | Description |
|------|----------|-------------|
| `file_read` | OpenHands `FileEditorExecutor` | Read file contents |
| `grep_search` | OpenHands `GrepExecutor` | Search patterns in files |
| `list_files` | OpenHands `GlobExecutor` | List files matching glob pattern |
| `web_fetch` | Custom `WebFetchExecutor` | Fetch URL content |

**Note:** Research tools always use OpenHands executors, regardless of the configured worker tool (claude_code, openhands, google_adk).

## Research Execution Flow

```
┌──────────────┐    ┌───────────────────┐    ┌──────────────────────┐
│  Pipeline    │    │  LiteLLMResearch  │    │  ResearchTool        │
│  Steps       │    │  Adapter          │    │  Registry            │
└──────┬───────┘    └─────────┬─────────┘    └──────────┬───────────┘
       │                      │                         │
       │ 1. StartResearch     │                         │
       │──────────────────────►                         │
       │   emit ResearchStarted                         │
       │                      │                         │
       │ 2. RunResearchSession│                         │
       │──────────────────────►                         │
       │                      │                         │
       │                      │ 3. LLM call with tools  │
       │                      │    (OpenAI format)      │
       │                      │                         │
       │                      │ 4. Tool call: grep_search
       │                      │─────────────────────────►
       │                      │                         │ GrepExecutor
       │                      │◄────────────────────────┤
       │                      │    results              │
       │                      │                         │
       │                      │ 5. Tool call: file_read │
       │                      │─────────────────────────►
       │                      │                         │ FileEditorExecutor
       │                      │◄────────────────────────┤
       │                      │    file contents        │
       │                      │                         │
       │                      │ 6. LLM synthesizes      │
       │◄─────────────────────┤    findings             │
       │   ResearchResult     │                         │
       │                      │                         │
       │ 7. ApplyResearchResult                         │
       │   emit ResearchCompleted                       │
       │   emit RoleTransitioned                        │
       │                      │                         │
┌──────▼───────┐
│  Agent now   │
│  MANAGER     │──► Continues with research context
└──────────────┘
```

## Research Findings Injection

After the RESEARCHER phase completes, findings are injected into the MANAGER's decomposition prompt:

```
┌─────────────────────────────────────────────────────────────────┐
│                    MANAGER PROMPT STRUCTURE                      │
├─────────────────────────────────────────────────────────────────┤
│  <ROLE>                                                          │
│  You are a MANAGER agent...                                      │
│  </ROLE>                                                         │
│                                                                  │
│  <RESEARCH_FINDINGS>           ◄── Injected if available         │
│  Found auth module in src/auth/, uses JWT tokens.                │
│  Key files: handler.py, middleware.py                            │
│  </RESEARCH_FINDINGS>                                            │
│                                                                  │
│  <DECOMPOSITION_STRATEGY>                                        │
│  ...                                                             │
│  </DECOMPOSITION_STRATEGY>                                       │
│                                                                  │
│  <TASK>                                                          │
│  Implement authentication                                        │
│  </TASK>                                                         │
└─────────────────────────────────────────────────────────────────┘
```

**Implementation:**

1. `agent.research_findings` property stores findings after `ResearchCompleted` event
2. `BuildDecompositionPrompt` step passes `agent.research_findings` to prompt builder
3. `build_manager_decomposition_prompt` conditionally renders `core/context/research.j2`
4. Template wraps findings in `<RESEARCH_FINDINGS>` tag

## Domain Events

### ResearchStarted
```python
class ResearchStarted(DomainEvent):
    tools_available: list[str]  # ["file_read", "grep_search", "list_files", "web_fetch"]
```

### ResearchCompleted
```python
class ResearchCompleted(DomainEvent):
    findings: str
    tool_calls_count: int
    gathered_context: dict[str, Any]
```

### RoleTransitioned
```python
class RoleTransitioned(DomainEvent):
    from_role: str   # "researcher"
    to_role: str     # "manager"
    reason: str
```

## Design Decisions

### 1. Single Source of Truth for Tool Names

Tool names are defined in `core/ports/research_port.py`:

```python
RESEARCH_TOOL_NAMES = frozenset({"file_read", "grep_search", "list_files", "web_fetch"})
```

The infrastructure layer validates against this contract at import time.

### 2. OpenHands for All Research Tools

Regardless of worker configuration, research always uses OpenHands executors:

- **Rationale:** Read-only operations have consistent behavior across implementations
- **Benefit:** Battle-tested implementations with edge case handling
- **Trade-off:** Adds OpenHands dependency even if worker uses different tool

### 3. Prompt Separation

The `researcher.j2` template is generic with no domain-specific content:

- No CVE references
- No security-specific language
- Domain context comes from task description, not role template

### 4. Single Responsibility Principle

| Component | Responsibility |
|-----------|----------------|
| `adapter.py` | LLM orchestration, tool calling loop |
| `tool_registry.py` | Tool definitions, execution routing |
| `tools/web_fetch.py` | HTTP fetching implementation |

## Usage Example

When a task like "Analyze the authentication system" is evaluated as complex and needing research:

1. **ComplexityEvaluated** with `needs_research=true` → Agent becomes RESEARCHER
2. **ResearchStarted** → Agent status becomes IN_PROGRESS
3. **Tool Calling Loop:**
   - LLM decides to call `grep_search(pattern="auth", include="*.py")`
   - LLM decides to call `file_read(path="src/auth/handler.py")`
   - LLM synthesizes findings
4. **ResearchCompleted** → Findings stored in agent
5. **RoleTransitioned** → Agent becomes MANAGER with research context
6. **Task Decomposition** → Manager decomposes with informed context
