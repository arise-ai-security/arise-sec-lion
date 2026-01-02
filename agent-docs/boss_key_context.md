# Design Choice 6: Boss Key Context Passing

This design builds on top of Design Choice 5 (Worker Report Context Passing) by introducing **structured extraction of key information** from the boss agent's original prompt (typically messy user input) and broadcasting it to all descendant agents.

## Problem Statement

**Need**: Better utilization of the boss agent's original context.

Based on observations from the [Worker Report Context system](./worker_report_context.md), we find that:

- **The boss agent always contains the most critical context**: bug reports, Dockerfiles, build scripts, relevant file paths, error messages, and reproduction steps
- **User input is typically unstructured**: Important details are scattered throughout free-form text
- **This is the only user interaction point**: Users can only input their requirements through the boss prompt; specific requests must be honored by all agents
- **Workers re-discover information**: Sub-agents often spend effort finding information that was already present in the original prompt
- **Verbatim usage is essential**: Agents should not create their own build scripts or look at irrelevant files when specific ones are provided

### Observed Problem Example

```
User Input (Boss Context):
  "Fix CVE-2024-50665 in gpac. The bug is at drm_sample.c:1562.
   Build with: ./configure --enable-sanitizer && make -j24
   Use commit 5d70253. The PoC is at testcase/poc7gpac."
         │
         ▼
Worker 1: Searches entire codebase for vulnerable file  ← REDUNDANT (file was given)
Worker 2: Creates custom build script                   ← WRONG (build command was given)
Worker 3: Uses master branch                            ← WRONG (commit was specified)
```

## Strategy Overview

Extract **structured key information** from the boss context (messy user input) into well-defined sections that are broadcast to all descendant agents via SharedExecutionContext:

| Section | Purpose |
|---------|---------|
| Bug/Issue Summary | Concise description of the problem |
| Error Messages | Actual error output, stack traces, sanitizer logs |
| Reproduction Steps | Exact commands to reproduce the issue |
| Referenced Files | Specific files mentioned by the user |
| Commit/Version References | Git commits, version tags, branch names |
| Related URLs | GitHub issues, CVE links, documentation |
| Environment | OS, compiler, library versions |
| Dependencies | Required packages and build tools |
| Key Facts & Requirements | User-specified constraints and requirements |

### Context Flow Diagram

```
                    ┌──────────────────────────────────────────────┐
                    │          SharedExecutionContext              │
                    │        (Boss Key Context Dashboard)          │
                    │                                              │
                    │  ┌─────────────────────────────────────────┐ │
                    │  │ [SOURCE CONTEXT] From Original Prompt   │ │
                    │  │                                         │ │
                    │  │ Bug Summary: SEGV at drm_sample.c:1562  │ │
                    │  │ Error: AddressSanitizer: SEGV on 0x0    │ │
                    │  │ Build: ./configure && make -j24         │ │
                    │  │ Commit: 5d70253                         │ │
                    │  │ Files: drm_sample.c, poc7gpac           │ │
                    │  └─────────────────────────────────────────┘ │
                    └──────────────────────────────────────────────┘
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              │                          │                          │
              ▼                          ▼                          ▼
       ┌─────────────┐          ┌─────────────┐            ┌─────────────┐
       │    BOSS     │          │   MANAGER   │            │   WORKER    │
       │ (extracts)  │          │ (inherits)  │            │ (inherits)  │
       └─────────────┘          └─────────────┘            └─────────────┘
              │                          │                          │
              │ All agents use the SAME build command, commit, files │
              └──────────────────────────┴──────────────────────────┘
```

## BossKeyContext Value Object

```python
# core/domain/values/context/boss_key_context.py
class BossKeyContext(BaseModel):
    """Structured key information extracted from boss prompt (Design Choice 6).

    This context is extracted once by the BOSS agent and broadcast to all
    descendant agents via SharedExecutionContext.
    """

    model_config = {"frozen": True}

    # Issue identification
    bug_summary: str = ""              # Concise problem description
    error_messages: tuple[str, ...] = ()  # Actual error output, stack traces

    # Reproduction information
    reproduction_steps: tuple[str, ...] = ()  # Exact commands to reproduce
    poc_command: str = ""              # Command to trigger vulnerability

    # Code references
    referenced_files: tuple[str, ...] = ()   # Specific files from user input
    vulnerable_location: str = ""      # file:line if specified

    # Version control
    commit_references: tuple[str, ...] = ()  # Git commits, tags, branches
    repository_url: str = ""           # Clone URL if provided

    # External references
    related_urls: tuple[str, ...] = ()      # GitHub issues, CVE links
    cve_id: str = ""                   # CVE identifier if applicable

    # Build environment
    environment: str = ""              # OS, compiler versions
    dependencies: tuple[str, ...] = ()     # Required packages
    build_command: str = ""            # Exact build command
    configure_command: str = ""        # Configuration command

    # Requirements
    key_facts: tuple[str, ...] = ()        # Important constraints
    user_requirements: tuple[str, ...] = () # Explicit user requests
    language: str = ""                 # Primary programming language
    project_name: str = ""             # Project identifier
```

## Extraction Process

### Two-Phase Extraction

| Phase | Actor | Action |
|-------|-------|--------|
| **Phase 1** | LLM | Parses unstructured user input, extracts structured fields |
| **Phase 2** | BOSS | Publishes BossKeyContext to SharedExecutionContext |

### LLM Extraction Prompt

```xml
<EXTRACT_KEY_CONTEXT>
Analyze the following user input and extract structured key information.
Preserve EXACT values for commands, file paths, commits, and URLs.

<categories>
- bug_summary: One-sentence description of the issue
- error_messages: Actual error output (preserve formatting)
- reproduction_steps: Numbered steps to reproduce
- referenced_files: File paths mentioned by user
- commit_references: Git commits, tags, branch names
- related_urls: GitHub issues, CVE links, documentation URLs
- environment: OS, compiler, library versions
- dependencies: Required packages (e.g., "build-essential", "libz-dev")
- build_command: Exact build command (e.g., "make -j24")
- configure_command: Configuration command (e.g., "./configure --enable-sanitizer")
- key_facts: Important constraints and facts
- language: Primary programming language
</categories>

User Input:
{{ boss_task_description }}
</EXTRACT_KEY_CONTEXT>
```

### Example Extraction

**Input (messy user prompt):**
```
Fix the segfault in gpac. The issue is CVE-2024-50665, it's a null pointer
dereference. Look at src/isomedia/drm_sample.c around line 1562. I've included
the stack trace from AddressSanitizer:

==1963314==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000
#0 0x7f4f7ad3f484 in isom_cenc_get_sai_by_saiz_saio drm_sample.c:1562:96
#1 0x7f4f7ad3f484 in gf_isom_cenc_get_sample_aux_info drm_sample.c:1672:10

To reproduce:
1. git clone https://github.com/gpac/gpac.git
2. cd gpac && git checkout 5d70253
3. ./configure --enable-sanitizer && make -j24
4. ./bin/gcc/MP4Box -dash 1000 testcase/poc7gpac

Environment: ubuntu:20.04, gcc 9.4.0, clang 10.0.0
Dependencies: build-essential, pkg-config, libz-dev
```

**Output (structured):**
```python
BossKeyContext(
    bug_summary="Null pointer dereference (SEGV) in gpac at drm_sample.c:1562",
    error_messages=(
        "==1963314==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000",
        "#0 0x7f4f7ad3f484 in isom_cenc_get_sai_by_saiz_saio drm_sample.c:1562:96",
        "#1 0x7f4f7ad3f484 in gf_isom_cenc_get_sample_aux_info drm_sample.c:1672:10",
    ),
    reproduction_steps=(
        "git clone https://github.com/gpac/gpac.git",
        "cd gpac && git checkout 5d70253",
        "./configure --enable-sanitizer && make -j24",
        "./bin/gcc/MP4Box -dash 1000 testcase/poc7gpac",
    ),
    poc_command="./bin/gcc/MP4Box -dash 1000 testcase/poc7gpac",
    referenced_files=(
        "src/isomedia/drm_sample.c",
        "testcase/poc7gpac",
    ),
    vulnerable_location="src/isomedia/drm_sample.c:1562",
    commit_references=("5d70253",),
    repository_url="https://github.com/gpac/gpac.git",
    related_urls=(),
    cve_id="CVE-2024-50665",
    environment="ubuntu:20.04, gcc 9.4.0, clang 10.0.0",
    dependencies=("build-essential", "pkg-config", "libz-dev"),
    build_command="make -j24",
    configure_command="./configure --enable-sanitizer",
    key_facts=("Null pointer dereference", "Issue is in isom_cenc_get_sai_by_saiz_saio"),
    language="c++",
    project_name="gpac",
)
```

## Inherited Context for Descendants

All descendant agents inherit the BossKeyContext via SharedExecutionContext:

### InheritedBossContext Template

```xml
<SOURCE_CONTEXT from="Original Prompt">
The following key information was extracted from the original task.
Use these EXACT values - do not substitute or re-discover.

<bug_summary>{{ boss_key_context.bug_summary }}</bug_summary>

{% if boss_key_context.error_messages %}
<error_messages>
{% for msg in boss_key_context.error_messages %}
{{ msg }}
{% endfor %}
</error_messages>
{% endif %}

{% if boss_key_context.reproduction_steps %}
<reproduction_steps>
{% for step in boss_key_context.reproduction_steps %}
{{ loop.index }}. {{ step }}
{% endfor %}
</reproduction_steps>
{% endif %}

{% if boss_key_context.referenced_files %}
<referenced_files>
{% for file in boss_key_context.referenced_files %}
- {{ file }}
{% endfor %}
</referenced_files>
{% endif %}

{% if boss_key_context.commit_references %}
<version_references>
{% for commit in boss_key_context.commit_references %}
- {{ commit }}
{% endfor %}
</version_references>
{% endif %}

{% if boss_key_context.build_command %}
<build_instructions>
Configure: {{ boss_key_context.configure_command }}
Build: {{ boss_key_context.build_command }}
</build_instructions>
{% endif %}

{% if boss_key_context.environment %}
<environment>{{ boss_key_context.environment }}</environment>
{% endif %}

{% if boss_key_context.dependencies %}
<dependencies>
{% for dep in boss_key_context.dependencies %}
{{ dep }}{% if not loop.last %}, {% endif %}
{% endfor %}
</dependencies>
{% endif %}

{% if boss_key_context.key_facts %}
<key_facts>
{% for fact in boss_key_context.key_facts %}
- {{ fact }}
{% endfor %}
</key_facts>
{% endif %}
</SOURCE_CONTEXT>
```

## Implementation Details

### Event Flow

```
1. BOSS Created
   └─► User provides task description (messy input)

2. ExtractBossKeyContext Step (before decomposition)
   └─► LLM extracts structured BossKeyContext
   └─► BossKeyContextExtracted event

3. Publish to SharedExecutionContext
   └─► context.publish_boss_key_context(boss_key_context)
   └─► BossKeyContextPublished event

4. All Descendants Inherit
   └─► InjectBossKeyContext step reads from SharedExecutionContext
   └─► Context added to ContextComposer
   └─► Rendered in prompt via boss_key_context.j2 template
```

### Domain Events

| Event | Purpose | Key Fields |
|-------|---------|------------|
| `BossKeyContextExtracted` | Key context extracted from boss prompt | `root_id`, `boss_key_context` |
| `BossKeyContextPublished` | Context published to shared dashboard | `root_id`, `published_by` |
| `BossKeyContextInherited` | Descendant received boss context | `agent_id`, `inherited_from_root` |

### Pipeline Integration

**1. Boss Pipeline** - Extracts and publishes key context:

```python
# core/application/pipelines.py - create_boss_pipeline()
steps = [
    ValidateBossAgent,
    ExtractBossKeyContext(llm_port),     # Design Choice 6: Extract
    PublishBossKeyContext(shared_context_port),  # Design Choice 6: Publish
    BuildDecompositionPrompt(prompt_builder),
    RunDecomposition(llm_port),
    ...
]
```

**2. InjectBossKeyContext Step** - Injects into all descendant prompts:

```python
# core/application/pipeline/steps/boss_context.py
class InjectBossKeyContext:
    """Inject boss key context into descendant prompts (Design Choice 6).

    All agents (MANAGER, WORKER) inherit this context to ensure
    they use the EXACT values from the original user input.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        # Skip for BOSS (it extracts, doesn't inherit)
        if state.agent.role == AgentRole.BOSS:
            return StepResult.ok(state)

        # Fetch from SharedExecutionContext
        shared_context = await self._shared_context_port.get(state.root_id)
        boss_key_context = shared_context.get_boss_key_context()

        if boss_key_context is None:
            return StepResult.ok(state)

        # Add to composer for template rendering
        composer = state.context_composer or ContextComposer()
        composer.add(boss_key_context)
        return StepResult.ok(state.with_context_composer(composer))
```

## Benefits

### 1. Eliminated Re-Discovery

Workers use exact values from user input:

```
Before (Without DC6):
  Worker 1: "Where is the vulnerable file?" → searches codebase (5 min)
  Worker 2: "What build command?" → tries various options (10 min)

After (With DC6):
  All Workers: Read boss_key_context.referenced_files, build_command (0 min)
```

### 2. Consistent Execution

All agents use the same configuration:

```
Boss extracts:
  commit: "5d70253"
  build: "make -j24"
       │
       ▼
All Workers:
  ├─► Worker 1: git checkout 5d70253 ← CORRECT
  ├─► Worker 2: make -j24           ← CORRECT
  └─► Worker 3: uses poc7gpac       ← CORRECT
```

### 3. User Requirements Honored

Specific user requests are passed verbatim:

```
User: "Do NOT modify isoffin_read_ch.c, only fix drm_sample.c"
       │
       ▼
key_facts: ("Only fix drm_sample.c", "Do not modify isoffin_read_ch.c")
       │
       ▼
All Workers see this constraint and comply
```

### 4. Single Source of Truth

No conflicting interpretations of user input:

```
SharedExecutionContext
       │
       │  BossKeyContext (authoritative)
       │
       └─► All agents read the SAME structured data
```

## Relationship to Other Design Choices

| Design Choice | Relationship |
|---------------|--------------|
| **DC4** (Thinker Justification) | DC6 provides global context; DC4 provides per-task justification |
| **DC5** (Worker Report) | DC6 is top-down broadcast; DC5 is bottom-up feedback |
| **DC7** (Inferred CWE) | DC7 builds on DC6's extracted context to infer CWE patterns |

### Evolution of Context Passing

```
DC4: "Here's why this task was assigned"
     (Parent → Child, task-specific)
       │
       ▼
DC5: "Here's what I discovered"
     (Child → Parent → Siblings, runtime)
       │
       ▼
DC6: "Here's the original source context"
     (Boss → ALL descendants, global broadcast)
```

## Configuration

```yaml
# config/config.yaml
orchestration:
  # Boss key context extraction (Design Choice 6)
  boss_key_context:
    enabled: true              # Extract and broadcast boss context
    extraction_model: "gpt-4o" # Model for structured extraction
```

```python
# config/settings.py
class OrchestrationConfig(BaseModel):
    class BossKeyContextConfig(BaseModel):
        """Boss key context extraction settings (Design Choice 6)."""

        enabled: bool = True
        extraction_model: str = "gpt-4o"

    boss_key_context: BossKeyContextConfig = BossKeyContextConfig()
```

## See Also

- [Design Choice 5: Worker Report Context](./worker_report_context.md) - Bottom-up context passing
- [Design Choice 7: Inferred CWE Context](./inferred_cwe_context.md) - CWE pattern inference
- [Context Passing Mechanism](./context-passing-mechanism.md) - Underlying SharedContext system
- [Context Passing Guide](./context-passing-guide.md) - Practical usage patterns
