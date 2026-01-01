# Context Passing Guide

Practical guide for passing context between agents using the ContextComposer API.

> **Reference:** See [Context Passing Mechanism](context-passing-mechanism.md) for the underlying SharedExecutionContext system.

---

## Quick Start

```python
from core.application.services.context_composer import ContextComposer
from core.domain.values.context import ParentSummary, SiblingResults, CustomContext

# Create composer
composer = ContextComposer()

# Add context data
composer.add(ParentSummary(
    task="Analyze authentication module",
    result="Found SQL injection vulnerability",
    role="manager",
))

# Build template variables
template_vars = composer.build()
# → {"parent_summary": {"task": "...", "result": "...", "role": "manager"}}
```

---

## Context Data Types

| Type | Template Key | Use Case |
|------|--------------|----------|
| `ParentSummary` | `parent_summary` | Child sees parent's task/result |
| `AncestryChain` | `ancestry_chain` | Full lineage from root |
| `AncestorData` | `ancestor_{label}` | Specific ancestor's context |
| `SiblingResults` | `sibling_results` | Worker coordination |
| `ChildOutcomes` | `child_outcomes` | Parent sees children's results |
| `SharedDecisions` | `shared_decisions` | Global execution decisions |
| `SharedArtifacts` | `shared_artifacts` | Shared outputs |
| `GlobalConfig` | `global_config` | JSON configuration |
| `CustomContext` | `custom_{label}` | Arbitrary user data |

---

## Common Scenarios

### 1. Global Configuration

Pass configuration to all agents:

```python
from core.domain.values.context import GlobalConfig

composer.add(GlobalConfig(data={
    "target": {"host": "192.168.1.100", "ports": [80, 443]},
    "timeout_seconds": 300,
}))
```

Template:
```jinja2
{% if global_config %}
Target: {{ global_config.target.host }}
{% endif %}
```

### 2. Parent → Child Context

Child receives parent's summary:

```python
from core.domain.values.context import ParentSummary

composer.add(ParentSummary(
    task="Develop exploit for CVE-2023-1234",
    result="Identified buffer overflow in parse_input()",
    decisions=("use_ret2libc",),
    role="manager",
))
```

### 3. Sibling Coordination

Workers see each other's status:

```python
from core.domain.values.context import SiblingResults, SiblingEntry

composer.add(SiblingResults(siblings=(
    SiblingEntry(
        agent_id="worker-1",
        index=0,
        status="completed",
        task_summary="Set up test environment",
        result_summary="Docker container running",
    ),
    SiblingEntry(
        agent_id="worker-2",
        index=1,
        status="in_progress",
        task_summary="Run exploit PoC",
    ),
)))
```

### 4. Child → Parent Results

Parent aggregates children's outcomes:

```python
from core.domain.values.context import ChildOutcomes, ChildOutcomeEntry

composer.add(ChildOutcomes(outcomes=(
    ChildOutcomeEntry(
        child_id="worker-123",
        task_summary="Develop exploit",
        result_text="Created poc.py with buffer overflow",
        artifacts=("poc.py",),
        status="completed",
    ),
)))
```

### 5. Custom Context

Add domain-specific data:

```python
from core.domain.values.context import CustomContext

composer.add(CustomContext(
    label="vulnerability_info",
    data={
        "cve_id": "CVE-2023-1234",
        "severity": "critical",
        "affected_versions": ["1.0", "1.1", "1.2"],
    },
))
```

Template:
```jinja2
{% if custom_vulnerability_info %}
CVE: {{ custom_vulnerability_info.cve_id }}
Severity: {{ custom_vulnerability_info.severity }}
{% endif %}
```

---

## Pipeline Integration

ContextComposer integrates with the pipeline system:

```python
from core.application.pipeline.context import PipelineState

class InjectCustomContext:
    async def execute(self, state: PipelineState) -> StepResult:
        composer = state.context_composer or ContextComposer()

        composer.add(CustomContext(
            label="my_data",
            data={"key": "value"},
        ))

        return StepResult.ok(state.with_context_composer(composer))
```

---

## Factory Functions

Use factory functions to build context from domain objects:

```python
from core.application.services.context_factories import (
    parent_summary_from_agent,
    sibling_results_from_agents,
    shared_decisions_from_context,
    child_outcomes_from_events,
)

# Build from agent
parent_ctx = parent_summary_from_agent(parent_agent)

# Build from sibling list
sibling_ctx = sibling_results_from_agents(siblings, current_index=2)

# Build from SharedExecutionContext
decisions_ctx = shared_decisions_from_context(shared_context)
```

---

## Template Access

All context data becomes available in Jinja2 templates:

```jinja2
{# Parent context #}
{% if parent_summary %}
Parent task: {{ parent_summary.task }}
Parent result: {{ parent_summary.result }}
{% endif %}

{# Sibling coordination #}
{% if sibling_results %}
Completed siblings: {{ sibling_results.completed_count }}/{{ sibling_results.total_count }}
{% for sibling in sibling_results.siblings %}
  - {{ sibling.task_summary }}: {{ sibling.status }}
{% endfor %}
{% endif %}

{# Global config #}
{% if global_config %}
Target: {{ global_config.target.host }}
{% endif %}

{# Custom data #}
{% if custom_vulnerability_info %}
CVE: {{ custom_vulnerability_info.cve_id }}
{% endif %}
```

---

## Best Practices

1. **Use typed context**: Prefer specific types (`ParentSummary`, `SiblingResults`) over `CustomContext` when possible
2. **Immutability**: All context data types are frozen - create new instances instead of mutating
3. **Pipeline steps**: Add context via pipeline steps for consistent integration
4. **Template guards**: Always check `{% if context_var %}` before accessing
5. **Label conventions**: Use snake_case labels for `CustomContext` and `AncestorData`
