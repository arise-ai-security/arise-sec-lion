# Design Choice 7: Inferred CWE Context Passing

This design builds on top of Design Choice 6 (Boss Key Context Passing) by introducing **automatic inference of CWE patterns** from the boss context and providing targeted fix strategies to all descendant workers.

## Problem Statement

**Need**: Better utilization of security knowledge base for vulnerability remediation.

Based on observations from the [Boss Key Context system](./boss_key_context.md), we find that:

- **Many bug fixes follow common CWE patterns**: Security vulnerabilities often have well-documented remediation strategies
- **Workers lack security domain knowledge**: Without CWE guidance, workers may apply ad-hoc fixes that don't address root causes
- **Fix patterns are repeatable**: Null pointer dereferences (CWE-476), buffer overflows (CWE-787), and SQL injections (CWE-89) all have established fix patterns
- **Sanitizers are CWE-specific**: Different vulnerability classes require different sanitizers for verification
- **Workers reinvent solutions**: Without pattern guidance, workers spend time discovering fix strategies that are already well-documented

### Observed Problem Example

```
Boss Context:
  "Fix null pointer dereference at drm_sample.c:1562"
         │
         ▼
Worker 1: Adds check `if (ptr != NULL)` but at wrong location  ← INEFFECTIVE
Worker 2: Wraps in try-catch (wrong language approach)         ← WRONG PATTERN
Worker 3: Eventually discovers correct null-guard pattern      ← SLOW
```

**With CWE Inference:**
```
Boss Context + CWE Analysis:
  Inferred: CWE-476 (NULL Pointer Dereference)
  Fix Pattern: "Add NULL check before dereferencing"
  Sanitizer: "-fsanitize=address" for verification
         │
         ▼
All Workers: Apply null-guard pattern at correct location ← EFFECTIVE
```

## Strategy Overview

Infer **CWE patterns** from the boss context (extracted in DC6) and provide targeted remediation guidance to all descendant agents:

| Section | Purpose |
|---------|---------|
| Inferred CWEs | List of likely CWE identifiers with confidence levels |
| Analysis Reasoning | Why each CWE was inferred from the context |
| Confidence Levels | How certain the inference is (high/medium/low) |
| Recommended Fix Patterns | CWE-specific remediation strategies |
| Recommended Sanitizers | Verification tools for each CWE class |
| CWE-Specific Guidance | Detailed fix templates from security knowledge base |

### Context Flow Diagram

```
                    ┌──────────────────────────────────────────────┐
                    │          SharedExecutionContext              │
                    │                                              │
                    │  ┌─────────────────────────────────────────┐ │
                    │  │ [CWE PATTERN ANALYSIS]                  │ │
                    │  │                                         │ │
                    │  │ Inferred CWEs: CWE-476 (high)           │ │
                    │  │ Reasoning: SEGV + null pointer mention  │ │
                    │  │ Fix Pattern: NULL check before deref    │ │
                    │  │ Sanitizer: -fsanitize=address           │ │
                    │  │ Verification: test, docker, config      │ │
                    │  └─────────────────────────────────────────┘ │
                    └──────────────────────────────────────────────┘
                                         │
                                         ▼
                    ┌────────────────────────────────────────────┐
                    │          Security Knowledge Base            │
                    │  CWE-476 → NULL check patterns              │
                    │  CWE-787 → Bounds checking patterns         │
                    │  CWE-89  → Parameterized query patterns     │
                    └────────────────────────────────────────────┘
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              │                          │                          │
              ▼                          ▼                          ▼
       ┌─────────────┐          ┌─────────────┐            ┌─────────────┐
       │   MANAGER   │          │   WORKER    │            │   WORKER    │
       │ (uses CWE   │          │ (applies    │            │ (verifies   │
       │  to plan)   │          │  fix)       │            │  with san)  │
       └─────────────┘          └─────────────┘            └─────────────┘
```

## InferredCWEContext Value Object

```python
# core/domain/values/context/inferred_cwe_context.py
class CWEInference(BaseModel):
    """Single CWE inference with reasoning and fix guidance."""

    model_config = {"frozen": True}

    cwe_id: str                        # e.g., "CWE-476"
    cwe_name: str = ""                 # e.g., "NULL Pointer Dereference"
    confidence: str = "medium"         # "high", "medium", "low"
    reasoning: str = ""                # Why this CWE was inferred

    # Fix guidance
    fix_pattern: str = ""              # Recommended remediation approach
    fix_template: str = ""             # Code template if applicable
    common_mistakes: tuple[str, ...] = ()  # Pitfalls to avoid

    # Verification
    recommended_sanitizers: tuple[str, ...] = ()  # e.g., ("-fsanitize=address",)
    verification_steps: tuple[str, ...] = ()      # How to verify the fix


class InferredCWEContext(BaseModel):
    """CWE patterns inferred from boss context (Design Choice 7).

    This context is inferred from the BossKeyContext (DC6) and provides
    security domain knowledge to guide remediation efforts.
    """

    model_config = {"frozen": True}

    # Inference results
    inferences: tuple[CWEInference, ...] = ()
    primary_cwe: str = ""              # Most likely CWE (highest confidence)

    # Analysis metadata
    analysis_reasoning: str = ""       # Overall reasoning for inference
    source_indicators: tuple[str, ...] = ()  # What triggered the inference

    # Aggregated guidance
    recommended_sanitizers: tuple[str, ...] = ()  # Combined from all CWEs
    verification_approach: str = ""    # Overall verification strategy
```

## CWE Inference Process

### Three-Phase Inference

| Phase | Actor | Action |
|-------|-------|--------|
| **Phase 1** | LLM | Analyzes BossKeyContext for CWE indicators |
| **Phase 2** | Knowledge Base | Enriches with fix patterns and templates |
| **Phase 3** | BOSS | Publishes InferredCWEContext to SharedExecutionContext |

### LLM Inference Prompt

```xml
<INFER_CWE_PATTERNS>
Analyze the following security context and infer applicable CWE patterns.
For each CWE, provide confidence level and reasoning.

<indicators_to_look_for>
- Error messages (SEGV, buffer overflow, SQL error, XSS, etc.)
- Vulnerability descriptions (null pointer, out-of-bounds, injection)
- Sanitizer output (AddressSanitizer, UBSan, ASan, MSan)
- CVE references (lookup known CWE mappings)
- Code patterns (unchecked pointers, strcpy, sprintf, raw SQL)
</indicators_to_look_for>

<common_cwe_patterns>
- CWE-476: NULL Pointer Dereference → SEGV, "null", "nullptr"
- CWE-787: Out-of-bounds Write → Buffer overflow, heap-buffer-overflow
- CWE-125: Out-of-bounds Read → Read past end, array index
- CWE-416: Use After Free → UAF, heap-use-after-free
- CWE-89: SQL Injection → SQL error, query, database
- CWE-79: XSS → Script injection, innerHTML, user input in HTML
- CWE-190: Integer Overflow → Wrap around, large allocation
</common_cwe_patterns>

Boss Key Context:
{{ boss_key_context | tojson }}

For each inferred CWE, provide:
- cwe_id: CWE identifier
- confidence: high/medium/low
- reasoning: What indicators led to this inference
- fix_pattern: High-level remediation approach
</INFER_CWE_PATTERNS>
```

### Example Inference

**Input (BossKeyContext from DC6):**
```python
BossKeyContext(
    bug_summary="Null pointer dereference (SEGV) in gpac at drm_sample.c:1562",
    error_messages=(
        "==1963314==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000",
        "#0 in isom_cenc_get_sai_by_saiz_saio drm_sample.c:1562:96",
    ),
    cve_id="CVE-2024-50665",
    ...
)
```

**Output (InferredCWEContext):**
```python
InferredCWEContext(
    inferences=(
        CWEInference(
            cwe_id="CWE-476",
            cwe_name="NULL Pointer Dereference",
            confidence="high",
            reasoning="AddressSanitizer reports SEGV on address 0x0, which is "
                      "the NULL address. Error message explicitly mentions "
                      "null pointer dereference.",
            fix_pattern="Add NULL check before dereferencing the pointer. "
                        "Ensure the pointer is validated at the earliest point "
                        "after assignment or function return.",
            fix_template="""
// Before:
value = ptr->field;  // Crashes if ptr is NULL

// After:
if (ptr == NULL) {
    return GF_BAD_PARAM;  // or appropriate error handling
}
value = ptr->field;
""",
            common_mistakes=(
                "Checking after dereference instead of before",
                "Only checking in one code path when multiple paths exist",
                "Returning without proper cleanup after NULL detection",
            ),
            recommended_sanitizers=("-fsanitize=address", "-fsanitize=undefined"),
            verification_steps=(
                "Run original PoC with fix - should not crash",
                "Run with AddressSanitizer - should report no errors",
                "Test NULL input explicitly",
            ),
        ),
    ),
    primary_cwe="CWE-476",
    analysis_reasoning="The bug report explicitly mentions 'null pointer dereference' "
                       "and AddressSanitizer output shows SEGV on address 0x0. "
                       "CVE-2024-50665 is a confirmed NULL pointer vulnerability.",
    source_indicators=(
        "SEGV on address 0x000000000000",
        "null pointer dereference in description",
        "AddressSanitizer error output",
    ),
    recommended_sanitizers=("-fsanitize=address", "-fsanitize=undefined"),
    verification_approach="Build with sanitizers, run PoC, verify no crash or sanitizer errors",
)
```

## Security Knowledge Base

The system maintains a knowledge base of CWE fix patterns:

### CWE Fix Pattern Registry

```python
# core/domain/security/cwe_patterns.py
CWE_FIX_PATTERNS: dict[str, CWEFixPattern] = {
    "CWE-476": CWEFixPattern(
        cwe_id="CWE-476",
        name="NULL Pointer Dereference",
        fix_strategy="Add NULL check before dereferencing",
        code_pattern="""
if (ptr == NULL) {
    // Handle error: return error code, log, or throw
    return ERROR_CODE;
}
// Safe to use ptr now
""",
        sanitizers=("-fsanitize=address", "-fsanitize=undefined"),
        verification="Run with AddressSanitizer, test NULL inputs",
    ),

    "CWE-787": CWEFixPattern(
        cwe_id="CWE-787",
        name="Out-of-bounds Write",
        fix_strategy="Add bounds checking before write operations",
        code_pattern="""
if (index >= 0 && index < array_size) {
    array[index] = value;  // Safe write
} else {
    // Handle out-of-bounds: error or clamp
}
""",
        sanitizers=("-fsanitize=address", "-fsanitize=bounds"),
        verification="Run with AddressSanitizer, test boundary values",
    ),

    "CWE-89": CWEFixPattern(
        cwe_id="CWE-89",
        name="SQL Injection",
        fix_strategy="Use parameterized queries instead of string concatenation",
        code_pattern="""
// Before (vulnerable):
query = "SELECT * FROM users WHERE id = " + user_input;

// After (safe):
query = "SELECT * FROM users WHERE id = ?";
cursor.execute(query, (user_input,));
""",
        sanitizers=("sqlmap", "manual testing"),
        verification="Test with SQL injection payloads, verify parameterization",
    ),
    # ... more patterns
}
```

## Inherited Context for Workers

All workers inherit the InferredCWEContext via SharedExecutionContext:

### InheritedCWEContext Template

```xml
<CWE_PATTERN_ANALYSIS>
Based on the original bug report, the following CWE patterns have been identified.
Use this guidance to apply the correct fix strategy.

<inferred_cwes>
{% for inference in inferred_cwe_context.inferences %}
<cwe id="{{ inference.cwe_id }}" confidence="{{ inference.confidence }}">
  <name>{{ inference.cwe_name }}</name>
  <reasoning>{{ inference.reasoning }}</reasoning>
</cwe>
{% endfor %}
</inferred_cwes>

<analysis_reasoning>
{{ inferred_cwe_context.analysis_reasoning }}
</analysis_reasoning>

<recommended_fix_patterns>
{% for inference in inferred_cwe_context.inferences %}
<pattern cwe="{{ inference.cwe_id }}">
{{ inference.fix_pattern }}

{% if inference.fix_template %}
<template>
{{ inference.fix_template }}
</template>
{% endif %}

{% if inference.common_mistakes %}
<avoid_mistakes>
{% for mistake in inference.common_mistakes %}
- {{ mistake }}
{% endfor %}
</avoid_mistakes>
{% endif %}
</pattern>
{% endfor %}
</recommended_fix_patterns>

<verification>
<sanitizers>
{% for san in inferred_cwe_context.recommended_sanitizers %}
{{ san }}
{% endfor %}
</sanitizers>
<approach>{{ inferred_cwe_context.verification_approach }}</approach>
</verification>
</CWE_PATTERN_ANALYSIS>
```

## Implementation Details

### Event Flow

```
1. BossKeyContext Extracted (DC6)
   └─► Bug summary, error messages, CVE ID available

2. InferCWEPatterns Step
   └─► LLM analyzes BossKeyContext for CWE indicators
   └─► Security knowledge base enriches with fix patterns
   └─► CWEPatternsInferred event

3. Publish to SharedExecutionContext
   └─► context.publish_inferred_cwe_context(inferred_cwe_context)
   └─► InferredCWEContextPublished event

4. All Descendants Inherit
   └─► InjectInferredCWEContext step reads from SharedExecutionContext
   └─► Context added to ContextComposer
   └─► Rendered in prompt via cwe_patterns.j2 template
```

### Domain Events

| Event | Purpose | Key Fields |
|-------|---------|------------|
| `CWEPatternsInferred` | CWE patterns identified from boss context | `root_id`, `inferred_cwes` |
| `InferredCWEContextPublished` | CWE context published to dashboard | `root_id`, `published_by` |
| `CWEContextInherited` | Worker received CWE guidance | `agent_id`, `primary_cwe` |

### Pipeline Integration

**1. Boss Pipeline** - Infers and publishes CWE context:

```python
# core/application/pipelines.py - create_boss_pipeline()
steps = [
    ValidateBossAgent,
    ExtractBossKeyContext(llm_port),           # DC6: Extract key info
    PublishBossKeyContext(shared_context_port),
    InferCWEPatterns(llm_port, cwe_knowledge_base),  # DC7: Infer CWE
    PublishInferredCWEContext(shared_context_port),   # DC7: Publish
    BuildDecompositionPrompt(prompt_builder),
    RunDecomposition(llm_port),
    ...
]
```

**2. InjectInferredCWEContext Step** - Injects into all worker prompts:

```python
# core/application/pipeline/steps/cwe_context.py
class InjectInferredCWEContext:
    """Inject inferred CWE context into worker prompts (Design Choice 7).

    Workers receive CWE-specific fix guidance and verification steps.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        # Skip for BOSS (it infers, doesn't inherit)
        if state.agent.role == AgentRole.BOSS:
            return StepResult.ok(state)

        # Fetch from SharedExecutionContext
        shared_context = await self._shared_context_port.get(state.root_id)
        inferred_cwe = shared_context.get_inferred_cwe_context()

        if inferred_cwe is None:
            return StepResult.ok(state)

        # Add to composer for template rendering
        composer = state.context_composer or ContextComposer()
        composer.add(inferred_cwe)
        return StepResult.ok(state.with_context_composer(composer))
```

## Benefits

### 1. Domain Knowledge Transfer

Workers receive expert-level security guidance:

```
Before (Without DC7):
  Worker: "How do I fix a null pointer bug?"
  → Trial and error, potentially incorrect fix

After (With DC7):
  Worker receives:
  → CWE-476 identification
  → "Add NULL check before dereferencing"
  → Code template
  → Common mistakes to avoid
  → Verification steps
```

### 2. Consistent Fix Quality

All workers apply the same proven patterns:

```
CWE-476 Fix Pattern:
  "Check pointer before use"
       │
       ├─► Worker 1 (fixer): Applies NULL check correctly
       ├─► Worker 2 (reviewer): Knows what to verify
       └─► Worker 3 (tester): Uses correct sanitizer
```

### 3. Reduced Re-Discovery

Workers don't need to research fix strategies:

```
Before (Without DC7):
  Worker 1: Searches for null pointer fix patterns (20 min)
  Worker 2: Reads CWE database (15 min)

After (With DC7):
  All Workers: Fix pattern already in context (0 min research)
```

### 4. Verification Guidance

Workers know exactly how to verify their fixes:

```
Inferred Guidance:
  Sanitizer: -fsanitize=address
  Verification: "Run PoC, verify no crash"
       │
       ▼
Worker: Builds with sanitizer, runs test, confirms fix
```

## Relationship to Other Design Choices

| Design Choice | Relationship |
|---------------|--------------|
| **DC5** (Worker Report) | Workers can report if CWE guidance was effective |
| **DC6** (Boss Key Context) | DC7 analyzes DC6's extracted data for CWE inference |
| **DC4** (Thinker Justification) | Justifications can reference CWE fix patterns |

### Evolution of Context Passing

```
DC6: "Here's the structured bug context"
     (Boss → ALL, source extraction)
       │
       ▼
DC7: "Based on that context, here's the CWE pattern and fix strategy"
     (Boss → ALL, security domain knowledge)
       │
       ▼
     Complete security-aware context cascade
```

## Configuration

```yaml
# config/config.yaml
orchestration:
  # Inferred CWE context (Design Choice 7)
  inferred_cwe_context:
    enabled: true                    # Enable CWE inference
    inference_model: "gpt-4o"        # Model for CWE inference
    confidence_threshold: "medium"   # Minimum confidence to include
    knowledge_base_path: "core/domain/security/cwe_patterns.yaml"
```

```python
# config/settings.py
class OrchestrationConfig(BaseModel):
    class InferredCWEConfig(BaseModel):
        """Inferred CWE context settings (Design Choice 7)."""

        enabled: bool = True
        inference_model: str = "gpt-4o"
        confidence_threshold: str = "medium"  # "high", "medium", "low"
        knowledge_base_path: str = "core/domain/security/cwe_patterns.yaml"

    inferred_cwe_context: InferredCWEConfig = InferredCWEConfig()
```

## Supported CWE Categories

The system provides fix patterns for common vulnerability classes:

| CWE | Name | Fix Pattern Summary |
|-----|------|---------------------|
| CWE-476 | NULL Pointer Dereference | Add NULL check before dereference |
| CWE-787 | Out-of-bounds Write | Add bounds checking before write |
| CWE-125 | Out-of-bounds Read | Add bounds checking before read |
| CWE-416 | Use After Free | Set pointer to NULL after free, use smart pointers |
| CWE-89 | SQL Injection | Use parameterized queries |
| CWE-79 | Cross-site Scripting (XSS) | Encode output, use CSP |
| CWE-190 | Integer Overflow | Use safe integer libraries, check before arithmetic |
| CWE-22 | Path Traversal | Validate and canonicalize paths |
| CWE-78 | OS Command Injection | Avoid shell, use safe APIs |
| CWE-502 | Deserialization | Use safe deserializers, validate input |

## See Also

- [Design Choice 6: Boss Key Context](./boss_key_context.md) - Source context extraction
- [Design Choice 5: Worker Report Context](./worker_report_context.md) - Worker feedback
- [Context Passing Mechanism](./context-passing-mechanism.md) - Underlying SharedContext system
- [Context Passing Guide](./context-passing-guide.md) - Practical usage patterns
