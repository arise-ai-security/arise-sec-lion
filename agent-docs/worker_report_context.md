# Design Choice 5: Worker Report Context Passing

This design builds on top of Design Choice 4 (Thinker Justification) by introducing **bottom-up context passing** from worker agents back to their parent thinker agents and the global shared context.

## Problem Statement

**Need**: Bidirectional feedback for online learning among all workers.

Based on observations from the [Thinker Justification system](./thinker_justification.md), we find that:

- Workers often **repeat work** already done by earlier coworkers (e.g., re-discovering the same line number in a file)
- Once workers start executing, **thinkers have finished planning** and cannot incorporate runtime insights
- Top-down context (Design Choice 4) only flows from parent to child, missing valuable **bottom-up feedback**
- No mechanism exists for **runtime knowledge sharing** between workers across the hierarchy

### Observed Redundancy Example

```
Parent Thinker: "Analyze CVE-2023-0770"
  │
  ├─► Worker 1: "Map vulnerable code path"
  │     └─► Discovers: vulnerable function at drm_sample.c:1562
  │
  └─► Worker 2: "Identify trigger conditions"
        └─► Re-discovers: same function at drm_sample.c:1562 ← REDUNDANT
```

## Strategy Overview

Pass **structured worker reports** from workers back to their parent thinker, who then **approves and publishes** key findings to the global shared context. This enables later workers to learn from earlier workers' discoveries.

### Context Flow Diagram

```
                    ┌──────────────────────────────────────────────┐
                    │          SharedExecutionContext              │
                    │       (Global Knowledge Dashboard)           │
                    └──────────────────────────────────────────────┘
                                         ▲
                                         │ 3. Publish approved knowledge
                    ┌────────────────────┴────────────────────────┐
                    │              THINKER (Manager)               │
                    │  - Reviews worker reports                    │
                    │  - Approves for global sharing               │
                    │  - Filters and curates insights              │
                    └────────────────────┬────────────────────────┘
                                         │
                    ┌────────────────────┼────────────────────────┐
            1. Submit│ report            │                2. Read │ context
                     ▼                   │                        ▼
              ┌─────────────┐     ┌─────────────┐         ┌─────────────┐
              │  WORKER 1   │     │  WORKER 2   │   ...   │  WORKER N   │
              │ (completed) │     │ (executing) │         │  (pending)  │
              └─────────────┘     └─────────────┘         └─────────────┘
```

### Two-Phase Knowledge Sharing

| Phase | Actor | Action | Purpose |
|-------|-------|--------|---------|
| **Phase 1** | Worker | Submits `WorkerReport` to parent | Direct feedback to supervisor |
| **Phase 2** | Thinker | Publishes approved knowledge to shared context | Curated global sharing |

**Why two phases?** Workers don't directly write to shared context because:
1. **Quality Control**: Supervisors can filter noise and curate valuable insights
2. **Future Rework**: Supervisors can detect issues and trigger re-execution
3. **Hierarchy Respect**: Maintains the BOSS → MANAGER → WORKER responsibility chain

## Worker Report Format

When a worker completes, it generates a structured report sent to its parent:

### WorkerReport Value Object

```python
# core/domain/values/worker_report.py
class WorkerReport(BaseModel):
    """Structured report from completed worker for context sharing."""

    model_config = {"frozen": True}

    # Task context
    original_task: str = ""           # The task assigned to this worker

    # Execution details
    approach: str = ""                 # How the task was executed
    observations: str = ""             # What was discovered during execution
    challenges_encountered: str = ""   # Difficulties encountered during execution

    # Outcomes
    deliverables: str = ""             # What was produced (files, artifacts)
    fulfillment_evidence: str = ""     # How thinker expectations were met
```

### Example Worker Report XML

```xml
<WORKER_REPORT worker_id="e286dfa6-..." timestamp="2025-12-30T23:39:00Z">
  <original_task>
    Map out the MP4 box structure that leads to the vulnerable code path
  </original_task>

  <alignment_with_justification>
    My work directly addresses the objective of identifying code conditions
    and variable states as outlined in your justification. By following
    the suggested approach of loading drm_sample.c and tracing call stacks,
    I ensured alignment with the expected deliverables.
  </alignment_with_justification>

  <approach>
    1. Loaded drm_sample.c lines 1400-1700 around the vulnerable line 1562
    2. Traced call stack from gf_isom_cenc_get_sample_aux_info()
    3. Mapped MP4 box types: moov → trak → mdia → minf → stbl → stsd
    4. Reviewed git commit 514a3af977f6 for context
  </approach>

  <observations>
    - Vulnerable path triggered when senc box missing auxiliary info offset
    - stsc (Sample-to-Chunk) box chunk_offset field directly influences path
    - NULL dereference occurs at line 1562 when pck->IV_size == 0
  </observations>

  <deliverables>
    Created path_map.md with:
    - Complete call stack trace (5 functions deep)
    - MP4 box hierarchy diagram
    - Variable state table at vulnerable line
    - Annotated code snippets
  </deliverables>

  <challenges>
    - Multiple code paths converge at vulnerable line; required tracing all callers
    - Some box types have optional fields; documented all variants
  </challenges>

  <fulfillment_evidence>
    Objective 'Deliver an exhaustive list of code conditions...' addressed:
    - Code conditions: 3 branch conditions identified
    - Variable states: IV_size, pck, senc_data documented
    - MP4 input fields: 7 relevant boxes mapped
  </fulfillment_evidence>
</WORKER_REPORT>
```

## Thinker's Published Knowledge

After reviewing worker reports, thinkers publish curated knowledge to the shared context:

### Published Knowledge Structure

```python
# core/domain/values/context/published_knowledge.py
class PublishedWorkerKnowledge(BaseModel):
    """Knowledge published by thinker based on worker report."""

    model_config = {"frozen": True}

    # Identification
    key: str                           # Unique key (usually task objective)
    worker_id: str                     # Source worker
    published_by: str                  # Approving thinker
    published_at: datetime             # Timestamp

    # Curated content
    objective: str                     # What the worker was asked to do
    why_assigned: str                  # Why this task was delegated
    how_accomplished: str              # Summary of approach and execution
    key_findings: list[str]            # Important discoveries
    deliverables: list[str]            # Artifacts produced
    relevance_to_siblings: str         # Why other workers should care
```

### Example Published Knowledge

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 📤 Context Published to Dashboard                                        │
├─────────────────────────────────────────────────────────────────────────┤
│ Key: Map out the MP4 box structure that leads to vulnerable code path    │
│                                                                          │
│ Objective:                                                               │
│   Map out the MP4 box structure that leads to the vulnerable code path.  │
│                                                                          │
│ Why Assigned:                                                            │
│   Understanding structural path to vulnerable code is prerequisite       │
│   for designing triggering MP4 input.                                    │
│                                                                          │
│ How It Was Accomplished:                                                 │
│   **Approach:** Loaded drm_sample.c, traced call stacks, mapped MP4 boxes│
│   **Deliverables:** path_map.md with call trace and box hierarchy        │
│   **Key Findings:**                                                      │
│     - Vulnerable line: drm_sample.c:1562                                 │
│     - Required boxes: moov → trak → mdia → minf → stbl → stsd → senc     │
│     - Trigger condition: IV_size == 0 with missing aux_info_offset       │
│                                                                          │
│ Relevance to Siblings:                                                   │
│   Later workers can directly use drm_sample.c:1562 location and          │
│   box hierarchy without re-discovering; spec writers should include      │
│   the identified box types and trigger conditions.                       │
│                                                                          │
│ Worker: e286dfa6... | Published: 12/30/2025, 11:39:00 PM                 │
└─────────────────────────────────────────────────────────────────────────┘
```

## Inherited Knowledge for Workers

Later workers inherit published knowledge from earlier workers via the shared context:

### CoworkerKnowledge Context Type

```python
# core/domain/values/context/data_types.py
class CoworkerKnowledgeEntry(BaseModel):
    """Single entry of knowledge published by an earlier coworker."""

    model_config = {"frozen": True}

    key: str                    # Task/objective key
    objective: str              # What the coworker was asked to do
    relevance: str              # Why this is relevant to current worker
    key_findings: tuple[str, ...]  # Important discoveries
    deliverables: tuple[str, ...]  # Artifacts produced
    source_worker_id: str       # Worker who discovered this
    published_by: str           # Thinker who approved and published


class CoworkerKnowledge(BaseModel):
    """Knowledge inherited from earlier coworkers (global shared context)."""

    model_config = {"frozen": True}

    entries: tuple[CoworkerKnowledgeEntry, ...] = ()
```

### Example Inherited Knowledge Prompt

```xml
<COWORKER_KNOWLEDGE count="2">
Earlier workers have published the following discoveries. Use this knowledge
to avoid redundant work and build on their findings.

<knowledge key="Map out MP4 box structure..." source="worker-e286dfa6">
  <objective>Map out the MP4 box structure that leads to the vulnerable code path.</objective>
  <relevance>This task requires understanding the structural path, which has been mapped.</relevance>
  <findings>
    - Vulnerable line: drm_sample.c:1562
    - Required boxes: moov → trak → mdia → minf → stbl → stsd → senc
    - Trigger: IV_size == 0 with missing aux_info_offset
  </findings>
  <deliverables>
    - path_map.md with call trace and box hierarchy
  </deliverables>
</knowledge>

<knowledge key="Identify memory layout..." source="worker-4b92c1a8">
  <objective>Identify the memory layout at the vulnerable dereference.</objective>
  <relevance>Memory layout informs payload construction for exploitation.</relevance>
  <findings>
    - Stack frame: 256 bytes, return address at offset 248
    - pck pointer stored at rbp-0x10
  </findings>
  <deliverables>
    - memory_layout.md with annotated stack diagram
  </deliverables>
</knowledge>
</COWORKER_KNOWLEDGE>
```

## Implementation Details

### Event Flow

```
1. Worker Executes
   └─► RunWorkerSession completes with result

2. GenerateWorkerReport Step
   └─► Creates WorkerReport, attaches to agent.pending_worker_report

3. WorkCompleted Event
   └─► Result and report propagated to parent via ParentNotificationService

4. ThinkerReviewAndPublish Step (in ParentNotificationService)
   └─► Quality gate: _is_valuable() filters noise
   └─► KnowledgePublished event to SharedExecutionContext

5. Later Workers Inherit
   └─► InjectCoworkerKnowledge fetches from SharedExecutionContext
   └─► CoworkerKnowledge injected via ContextComposer
```

### Domain Events

| Event | Purpose | Key Fields |
|-------|---------|------------|
| `WorkCompleted` | Worker done with structured report | `result`, `worker_report` |
| `KnowledgePublished` | Thinker publishes to global context | `key`, `content`, `published_by`, `source_worker` |
| `KnowledgeInherited` | Worker received sibling knowledge | `agent_id`, `inherited_keys` |

### Pipeline Integration

The implementation uses a two-pipeline approach with clear separation of concerns:

**1. Worker Pipeline** - Generates structured report and injects coworker knowledge:

```python
# core/application/pipelines.py - create_worker_pipeline()
steps = [
    ValidateWorkerAgent,
    StartWorkerExecution(),
    InjectThinkerJustification(),       # Design Choice 4
    InjectCoworkerKnowledge(shared_context_port),  # Design Choice 5: Read
    BuildWorkerPrompt(prompt_builder),
    EmitPromptSent(prompt_type="worker_execution", target="dynamic"),
    RunWorkerSession(worker_port),
    GenerateWorkerReport(),             # Design Choice 5: Generate report for parent
]
```

**2. InjectCoworkerKnowledge Step** - Injects earlier workers' discoveries:

```python
# core/application/pipeline/steps/knowledge.py
class InjectCoworkerKnowledge:
    """Inject coworker knowledge context into worker prompts (Design Choice 5).

    Note: Workers do NOT publish knowledge. Only parent thinkers can publish
    after reviewing worker reports. See ParentNotificationService.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        # Fetch published knowledge from SharedExecutionContext
        shared_context = await self._shared_context_port.get(state.root_id)
        all_knowledge = shared_context.get_all_knowledge()

        # Convert to CoworkerKnowledgeEntry objects for template rendering
        entries = tuple(
            CoworkerKnowledgeEntry(
                key=k.key, objective=k.objective, relevance=k.relevance,
                key_findings=k.key_findings, deliverables=k.deliverables,
                source_worker_id=str(k.source_worker_id),
                published_by=str(k.published_by),
            )
            for k in all_knowledge
        )

        composer = state.context_composer or ContextComposer()
        composer.add(CoworkerKnowledge(entries=entries))
        return StepResult.ok(state.with_context_composer(composer))
```

**3. GenerateWorkerReport Step** - Creates structured report after execution:

```python
# core/application/pipeline/steps/knowledge.py
class GenerateWorkerReport:
    """Generate a structured WorkerReport after worker execution (Design Choice 5).

    This step extracts key information from the worker's execution and creates
    a structured WorkerReport that will be included in the WorkCompleted event.
    The report is then sent to the parent thinker for review and potential
    publishing to the shared context.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        agent = state.agent
        if agent.result is None:
            return StepResult.ok(state)

        # Generate structured report from execution results
        report = WorkerReport.from_execution(
            original_task=agent.task_description or "",
            approach=_extract_approach(agent.result),
            observations=_extract_observations(agent.result),
            challenges_encountered=_extract_challenges(agent.result),
            deliverables=_extract_deliverables(agent.result),
            fulfillment_evidence=_build_fulfillment_evidence(
                agent.result, thinker_objective
            ),
        )

        # Attach report to agent for inclusion in WorkCompleted event
        agent.pending_worker_report = report
        return StepResult.ok(state)
```

### Parent Thinker Publishing Logic

**Knowledge publishing happens in `ParentNotificationService`** when a worker child completes. The parent thinker uses the `ThinkerReviewAndPublish` step to review and approve knowledge:

```python
# core/application/services/parent_notifier.py
class ParentNotificationService:
    """Handles parent notifications and knowledge publishing (Design Choice 5).

    Uses ThinkerReviewAndPublish step for the review/publish logic.
    """

    def __init__(self, ..., shared_context_port):
        # Design Choice 5: Create thinker review step if shared context is available
        self._thinker_review: ThinkerReviewAndPublish | None = None
        if shared_context_port is not None:
            self._thinker_review = ThinkerReviewAndPublish(shared_context_port)

    async def notify_if_complete(self, child: AgentSession) -> None:
        # ... handle child completion ...

        # Design Choice 5: Parent thinker publishes worker knowledge
        if (
            self._publish_worker_knowledge
            and child_succeeded
            and child.role == AgentRole.WORKER
            and self._thinker_review is not None
        ):
            root_id = child.hierarchy_limits.root_id if child.hierarchy_limits else None
            if root_id is not None:
                await self._thinker_review.execute(
                    child_agent_id=str(child.agent_id),
                    child_task=child.task_description or "",
                    child_result=child.result or "",
                    child_report=child.pending_worker_report,  # From GenerateWorkerReport
                    parent_agent_id=str(parent.agent_id),
                    root_id=str(root_id),
                )
```

**4. ThinkerReviewAndPublish Step** - Quality-gates and publishes knowledge:

```python
# core/application/pipeline/steps/knowledge.py
class ThinkerReviewAndPublish:
    """Thinker reviews worker report and publishes to shared context (Design Choice 5).

    This enforces the design principle: workers do NOT publish directly.
    Only parent thinkers can approve and publish knowledge.
    """

    async def execute(
        self,
        child_agent_id: str,
        child_task: str,
        child_result: str,
        child_report: WorkerReport | None,
        parent_agent_id: str,
        root_id: str,
    ) -> bool:
        shared_context = await self._shared_context_port.get(UUID(root_id))

        # Quality gate: Check if report is valuable enough to publish
        if not self._is_valuable(child_result, child_report):
            return False

        # Parent thinker publishes (approves) the knowledge
        shared_context.publish_knowledge(
            key=child_task[:100],
            objective=child_task,
            relevance=f"Completed: {child_task[:80]}...",
            key_findings=key_findings,
            deliverables=deliverables,
            source_worker_id=UUID(child_agent_id),
            published_by=UUID(parent_agent_id),
        )
        return True

    def _is_valuable(self, result: str, report: WorkerReport | None) -> bool:
        """Quality gate: Only publish meaningful knowledge, not noise."""
        if not result or len(result) < 50:
            return False
        # Check for indicators of valuable content
        valuable_indicators = ["file:", "line:", "found", "discovered", "created", ...]
        return any(indicator in result.lower() for indicator in valuable_indicators)
```

## Benefits

### 1. Reduced Redundant Work

Workers inherit discoveries from earlier siblings:

```
Before (Without DC5):
  Worker 1: Discovers vulnerable line at drm_sample.c:1562 (10 min)
  Worker 2: Re-discovers same line via grep (10 min)  ← WASTED

After (With DC5):
  Worker 1: Discovers vulnerable line at drm_sample.c:1562 (10 min)
  Worker 2: Inherits location, proceeds with analysis (2 min)  ← EFFICIENT
```

### 2. Online Learning

Unlike static planning, workers adapt based on runtime discoveries:

```
Planning Phase (DC4):          Execution Phase (DC5):
  Thinker plans subtasks         Workers discover new info
        │                              │
        ▼                              ▼
  Static justifications  →   Dynamic knowledge sharing
  (fixed at planning)        (evolves during execution)
```

### 3. Quality-Gated Sharing

Thinkers curate what gets published, preventing noise:

```
Worker Reports (raw):              Published Knowledge (curated):
  - Lots of tool output              - Key findings only
  - Trial and error logs             - Verified discoveries
  - Verbose execution traces         - Actionable insights
        │                                    │
        └─────── Thinker reviews ────────────┘
```

### 4. Future Rework Mechanism

Thinkers can detect issues and trigger re-execution:

```
Thinker reviews worker report:
  │
  ├─► Report satisfactory → Publish to shared context
  │
  └─► Report inadequate → Request rework (future feature)
        │
        └─► Worker re-executes with additional guidance
```

## Relationship to Other Design Choices

| Design Choice | Relationship |
|---------------|--------------|
| **DC4** (Thinker Justification) | DC5 completes the feedback loop; DC4 is top-down, DC5 is bottom-up |
| **DC3** (Budget Allocation) | Budget tracking helps identify which workers to prioritize for knowledge sharing |
| **DC2** (Complexity Budget) | Complex workers may produce more valuable knowledge worth publishing |

### Complete Context Flow

```
DC4: Top-Down Justification
     THINKER → WORKER
     (planning guidance)
           │
           │                    ┌────────────────────────────┐
           ▼                    │   SharedExecutionContext   │
     WORKER executes            │   (Global Knowledge)       │
           │                    └────────────────────────────┘
           │                                  ▲
           ▼                                  │
DC5: Bottom-Up Reporting                      │
     WORKER → THINKER ────────────────────────┘
     (execution feedback)         Publishes
```

### Evolution of Context Passing

```
DC4: "Here's what I expect you to do and why"
     (Parent → Child, planning time)
       │
       ▼
DC5: "Here's what I did and what I found"
     (Child → Parent → Siblings, execution time)
       │
       ▼
     Complete bidirectional feedback loop
```

## Configuration

```yaml
# config/config.yaml
orchestration:
  # Coworker knowledge sharing (Design Choice 5)
  # Enables workers to learn from earlier coworkers' discoveries via SharedExecutionContext.
  coworker_knowledge:
    enabled: true              # Enabled by default for online learning
    max_entries_per_worker: 10 # Maximum number of knowledge entries to inject per worker
```

```python
# config/settings.py
class OrchestrationConfig(BaseModel):
    class CoworkerKnowledgeConfig(BaseModel):
        """Coworker knowledge sharing settings (Design Choice 5)."""

        enabled: bool = True  # Enabled by default
        max_entries_per_worker: int = Field(default=10, ge=0)

    coworker_knowledge: CoworkerKnowledgeConfig = CoworkerKnowledgeConfig()
```

## Template Integration

### Worker Report Template

```jinja2
{# prompts/core/context/worker_report_request.j2 #}
{% if thinker_justification %}
<REPORT_FORMAT>
When you complete this task, provide a structured report including:
1. How your work aligns with thinker expectations
2. Approach taken and reasoning
3. Key observations and discoveries
4. Deliverables produced
5. Challenges encountered
6. Evidence that you fulfilled the objective

This report will be reviewed by your thinker and may be shared with
sibling workers to prevent redundant effort.
</REPORT_FORMAT>
{% endif %}
```

### Coworker Knowledge Template

```jinja2
{# prompts/core/context/coworker_knowledge.j2 #}
{% if coworker_knowledge and coworker_knowledge.entries %}
<COWORKER_KNOWLEDGE count="{{ coworker_knowledge.count }}">
Earlier workers have published the following discoveries. Use this knowledge
to avoid redundant work and build on their findings.

{% for entry in coworker_knowledge.entries %}
<knowledge key="{{ entry.key[:50] }}...">
<objective>{{ entry.objective }}</objective>
<relevance>{{ entry.relevance }}</relevance>
{% if entry.key_findings %}
<findings>
{% for finding in entry.key_findings %}
  - {{ finding }}
{% endfor %}
</findings>
{% endif %}
{% if entry.deliverables %}
<deliverables>
{% for deliverable in entry.deliverables %}
  - {{ deliverable }}
{% endfor %}
</deliverables>
{% endif %}
<source worker="{{ entry.source_worker_id }}" published_by="{{ entry.published_by }}"/>
</knowledge>
{% endfor %}
</COWORKER_KNOWLEDGE>
{% endif %}
```

## See Also

- [Design Choice 4: Thinker Justification](./thinker_justification.md) - Top-down context passing
- [Design Choice 3: Proportional Budget Allocation](./budgeted_tree_plus.md) - Resource allocation
- [Context Passing Mechanism](./context-passing-mechanism.md) - Underlying SharedContext system
- [Context Passing Guide](./context-passing-guide.md) - Practical usage patterns
