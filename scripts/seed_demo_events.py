#!/usr/bin/env python3
"""Seed the local database with realistic demo events for dashboard testing.

Constructs actual Pydantic event objects to guarantee correct payloads.

Usage:
    POSTGRES_PASSWORD=arise python scripts/seed_demo_events.py
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from core.domain.events.events import (
    AgentCreated,
    ChildCompleted,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    OperationFinished,
    ProbeCompleted,
    ProbeStarted,
    PromptSent,
    RetryScheduled,
    RunStarted,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    TokensConsumed,
    VerificationFailed,
    WorkCompleted,
    WorkerCostRecorded,
)
from core.domain.values.subtask import Subtask
from infrastructure.adapters.postgres_event_store import PostgresEventStore


async def main():
    store = PostgresEventStore("postgresql://arise:arise@localhost:5432/arise_events")
    await store.connect()

    # Clear existing data
    async with store.pool.acquire() as conn:
        await conn.execute("DELETE FROM events")

    now = datetime.now(UTC)
    seq: dict = {}  # aggregate_id -> next sequence number
    all_events = []

    def next_seq(agg_id):
        n = seq.get(agg_id, 0)
        seq[agg_id] = n + 1
        return n

    def add(agg_id, event_cls, offset, **kwargs):
        s = next_seq(agg_id)
        event = event_cls(
            aggregate_id=agg_id,
            sequence_number=s,
            occurred_at=now + timedelta(seconds=offset),
            **kwargs,
        )
        all_events.append((event, s))

    # =========================================================================
    # Agent IDs
    # =========================================================================
    boss_id = uuid4()
    mgr1_id, mgr2_id, mgr3_id = uuid4(), uuid4(), uuid4()
    w1_id, w2_id, w3_id, w4_id, w5_id, w6_id = (uuid4() for _ in range(6))

    task_desc = "Analyze and patch CVE-2024-12345: Remote code execution via deserialization in Apache Commons Collections"

    # Valid HeuristicConfig for AgentCreated.config
    heuristic_config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": 4000},
        "tool": "claude_code",
    }

    def subtask(desc, si=0, deps=None):
        return Subtask(description=desc, config=heuristic_config, depends_on=deps or [], sibling_index=si)

    # ----- BOSS -----
    add(boss_id, AgentCreated, 0, role="boss", config=heuristic_config, sibling_index=0, depends_on=[])
    add(boss_id, RunStarted, 0.1, task_description=task_desc, instance_id="CVE-2024-12345")
    add(boss_id, TaskAssigned, 0.5, task_description=task_desc)
    add(boss_id, StatusChanged, 1, old_status="pending", new_status="analyzing")
    add(boss_id, PromptSent, 1.5, prompt="<ROLE>You are a security vulnerability analyst.</ROLE>\n<TASK>Analyze and patch CVE-2024-12345: Remote code execution via deserialization in Apache Commons Collections</TASK>\n<INSTRUCTIONS>Evaluate task complexity. Respond with EXECUTE if simple enough for one agent, or DECOMPOSE with subtasks.</INSTRUCTIONS>", prompt_type="complexity_evaluation", target="llm")
    add(boss_id, TokensConsumed, 2, model="gpt-4o-mini", prompt_tokens=1200, completion_tokens=800, total_tokens=2000, cost_usd=0.0012, operation="complexity_evaluation")
    add(boss_id, ComplexityEvaluated, 2.5, complexity="high", determined_role="manager", reasoning="Multi-step vulnerability: requires analysis, patching, and verification")
    add(boss_id, OperationFinished, 3, operation_type="complexity_evaluation", duration_seconds=1.8)
    add(boss_id, PromptSent, 3.5, prompt="<ROLE>You are a task decomposition specialist for security vulnerability remediation.</ROLE>\n<TASK>Analyze and patch CVE-2024-12345: Remote code execution via deserialization in Apache Commons Collections</TASK>\n<INSTRUCTIONS>Break this task into independent subtasks. Each subtask should be completable by a single agent.</INSTRUCTIONS>\n<CONSTRAINTS>Maximum 5 subtasks. Each must have clear success criteria.</CONSTRAINTS>", prompt_type="task_decomposition", target="llm")
    add(boss_id, TokensConsumed, 4, model="gpt-4o-mini", prompt_tokens=2500, completion_tokens=1500, total_tokens=4000, cost_usd=0.0024, operation="task_decomposition")
    add(boss_id, SubtasksDefined, 5, subtasks=[
        subtask("Identify vulnerable deserialization entry points", 0),
        subtask("Generate and apply security patch", 1, [0]),
        subtask("Verify patch correctness and run regression tests", 2, [1]),
    ])
    add(boss_id, OperationFinished, 5.5, operation_type="task_decomposition", duration_seconds=3.2)
    add(boss_id, StatusChanged, 6, old_status="analyzing", new_status="waiting")
    add(boss_id, ChildSpawned, 7, child_id=mgr1_id, child_role="pending", subtask=subtask("Identify vulnerable deserialization entry points", 0), child_config=heuristic_config)
    add(boss_id, ChildSpawned, 7.5, child_id=mgr2_id, child_role="pending", subtask=subtask("Generate and apply security patch", 1, [0]), child_config=heuristic_config)
    add(boss_id, ChildSpawned, 8, child_id=mgr3_id, child_role="pending", subtask=subtask("Verify patch correctness and run regression tests", 2, [1]), child_config=heuristic_config)

    # ----- MANAGER 1: Analysis -----
    add(mgr1_id, AgentCreated, 8.5, role="pending", parent_id=boss_id, config=heuristic_config, sibling_index=0, depends_on=[])
    add(mgr1_id, TaskAssigned, 9, task_description="Identify vulnerable deserialization entry points in Apache Commons Collections")
    add(mgr1_id, StatusChanged, 9.5, old_status="pending", new_status="analyzing")
    add(mgr1_id, PromptSent, 9.8, prompt="<ROLE>You are a code analysis specialist.</ROLE>\n<TASK>Identify vulnerable deserialization entry points in Apache Commons Collections</TASK>\n<parent-context>Parent task: CVE-2024-12345 remediation. This is subtask 1 of 3: vulnerability identification.</parent-context>\n<INSTRUCTIONS>Evaluate complexity. EXECUTE if simple, DECOMPOSE if multi-step.</INSTRUCTIONS>", prompt_type="complexity_evaluation", target="llm")
    add(mgr1_id, TokensConsumed, 10, model="gpt-4o-mini", prompt_tokens=1800, completion_tokens=600, total_tokens=2400, cost_usd=0.0015, operation="complexity_evaluation")
    add(mgr1_id, ComplexityEvaluated, 11, complexity="medium", determined_role="manager", reasoning="Requires scanning multiple source files")
    add(mgr1_id, PromptSent, 11.5, prompt="<ROLE>You are a code analysis decomposition specialist.</ROLE>\n<TASK>Identify vulnerable deserialization entry points in Apache Commons Collections</TASK>\n<INSTRUCTIONS>Break into subtasks for parallel execution.</INSTRUCTIONS>", prompt_type="task_decomposition", target="llm")
    add(mgr1_id, TokensConsumed, 12, model="gpt-4o-mini", prompt_tokens=2200, completion_tokens=1200, total_tokens=3400, cost_usd=0.002, operation="task_decomposition")
    add(mgr1_id, SubtasksDefined, 13, subtasks=[
        subtask("Scan source for ObjectInputStream.readObject() calls", 0),
        subtask("Analyze transformer chain gadget classes", 1, [0]),
    ])
    add(mgr1_id, StatusChanged, 14, old_status="analyzing", new_status="waiting")
    add(mgr1_id, ChildSpawned, 14.5, child_id=w1_id, child_role="pending", subtask=subtask("Scan source for ObjectInputStream.readObject() calls", 0), child_config=heuristic_config)
    add(mgr1_id, ChildSpawned, 15, child_id=w2_id, child_role="pending", subtask=subtask("Analyze transformer chain gadget classes", 1, [0]), child_config=heuristic_config)

    # ----- WORKER 1 -----
    add(w1_id, AgentCreated, 15.5, role="pending", parent_id=mgr1_id, config=heuristic_config, sibling_index=0, depends_on=[])
    add(w1_id, TaskAssigned, 16, task_description="Scan source for ObjectInputStream.readObject() calls")
    add(w1_id, StatusChanged, 16.5, old_status="pending", new_status="analyzing")
    add(w1_id, TokensConsumed, 17, model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=400, total_tokens=1400, cost_usd=0.0008, operation="complexity_evaluation")
    add(w1_id, ComplexityEvaluated, 17.5, complexity="low", determined_role="worker", reasoning="Single grep-like task")
    add(w1_id, PromptSent, 17.8, prompt="<ROLE>You are a security code auditor.</ROLE>\n<TASK>Scan source for ObjectInputStream.readObject() calls</TASK>\n<sibling-context>You are worker 1 of 2. Sibling task: 'Analyze transformer chain gadget classes' (pending).</sibling-context>\n<INSTRUCTIONS>Search all Java source files for ObjectInputStream.readObject() calls and report file paths and line numbers.</INSTRUCTIONS>", prompt_type="worker_execution", target="claude_code")
    add(w1_id, ProbeStarted, 18, probe_type="repo_scan")
    add(w1_id, ProbeCompleted, 19, probe_type="repo_scan", result_summary="Found 3 Java source directories")
    add(w1_id, CodeGenerationStarted, 20, tool_name="claude_code")
    add(w1_id, StatusChanged, 20.5, old_status="analyzing", new_status="in_progress")
    add(w1_id, ThoughtCaptured, 22, content="Scanning src/ for ObjectInputStream usage...", stream="tool", output_type="progress")
    add(w1_id, ThoughtCaptured, 25, content="Found 5 files with readObject() calls:\n- InvokerTransformer.java:142\n- TransformedMap.java:98\n- LazyMap.java:65", stream="tool", output_type="output")
    add(w1_id, WorkerCostRecorded, 28, tool_name="claude_code", model="claude-sonnet-4-20250514", tokens=15000, cost_usd=0.045, duration_seconds=8.5)
    add(w1_id, OperationFinished, 28.5, operation_type="worker_execution", duration_seconds=8.5)
    add(w1_id, WorkCompleted, 29, result="Identified 5 vulnerable deserialization entry points in Apache Commons Collections transformer chain.")
    add(w1_id, StatusChanged, 29.5, old_status="in_progress", new_status="completed")
    add(mgr1_id, ChildCompleted, 30, child_id=w1_id, result="Identified 5 vulnerable deserialization entry points")

    # ----- WORKER 2 -----
    add(w2_id, AgentCreated, 15.5, role="pending", parent_id=mgr1_id, config=heuristic_config, sibling_index=1, depends_on=[0])
    add(w2_id, TaskAssigned, 30.5, task_description="Analyze transformer chain gadget classes")
    add(w2_id, StatusChanged, 31, old_status="pending", new_status="analyzing")
    add(w2_id, TokensConsumed, 31.5, model="gpt-4o-mini", prompt_tokens=1100, completion_tokens=500, total_tokens=1600, cost_usd=0.001, operation="complexity_evaluation")
    add(w2_id, ComplexityEvaluated, 32, complexity="low", determined_role="worker", reasoning="Code analysis task")
    add(w2_id, CodeGenerationStarted, 33, tool_name="claude_code")
    add(w2_id, StatusChanged, 33.5, old_status="analyzing", new_status="in_progress")
    add(w2_id, ThoughtCaptured, 34, content="Analyzing InvokerTransformer gadget chain...", stream="tool", output_type="thinking")
    add(w2_id, ThoughtCaptured, 38, content="The chain: ConstantTransformer -> InvokerTransformer -> ChainedTransformer allows arbitrary method invocation.", stream="tool", output_type="output")
    add(w2_id, WorkerCostRecorded, 42, tool_name="claude_code", model="claude-sonnet-4-20250514", tokens=18000, cost_usd=0.054, duration_seconds=12.0)
    add(w2_id, OperationFinished, 42.5, operation_type="worker_execution", duration_seconds=12.0)
    add(w2_id, WorkCompleted, 43, result="Transformer chain gadget analysis complete. Root cause: InvokerTransformer allows arbitrary method invocation.")
    add(w2_id, StatusChanged, 43.5, old_status="in_progress", new_status="completed")
    add(mgr1_id, ChildCompleted, 44, child_id=w2_id, result="Transformer chain gadget analysis complete")
    add(mgr1_id, WorkCompleted, 45, result="Vulnerability analysis complete: 5 entry points, root cause is InvokerTransformer gadget chain.")
    add(mgr1_id, StatusChanged, 45.5, old_status="waiting", new_status="completed")
    add(boss_id, ChildCompleted, 46, child_id=mgr1_id, result="Vulnerability analysis complete")

    # ----- MANAGER 2: Patch Generation -----
    add(mgr2_id, AgentCreated, 46.5, role="pending", parent_id=boss_id, config=heuristic_config, sibling_index=1, depends_on=[0])
    add(mgr2_id, TaskAssigned, 47, task_description="Generate and apply security patch for deserialization vulnerability")
    add(mgr2_id, StatusChanged, 47.5, old_status="pending", new_status="analyzing")
    add(mgr2_id, TokensConsumed, 48, model="gpt-4o-mini", prompt_tokens=2000, completion_tokens=700, total_tokens=2700, cost_usd=0.0016, operation="complexity_evaluation")
    add(mgr2_id, ComplexityEvaluated, 49, complexity="medium", determined_role="manager", reasoning="Requires code modification and compilation")
    add(mgr2_id, TokensConsumed, 50, model="gpt-4o-mini", prompt_tokens=2800, completion_tokens=1100, total_tokens=3900, cost_usd=0.0023, operation="task_decomposition")
    add(mgr2_id, SubtasksDefined, 51, subtasks=[
        subtask("Add deserialization filter to block dangerous classes", 0),
        subtask("Compile and verify patch doesn't break functionality", 1, [0]),
    ])
    add(mgr2_id, StatusChanged, 52, old_status="analyzing", new_status="waiting")
    add(mgr2_id, ChildSpawned, 52.5, child_id=w3_id, child_role="pending", subtask=subtask("Add deserialization filter", 0), child_config=heuristic_config)
    add(mgr2_id, ChildSpawned, 53, child_id=w4_id, child_role="pending", subtask=subtask("Compile and verify patch", 1, [0]), child_config=heuristic_config)

    # ----- WORKER 3 (patch writer — VerificationFailed + RetryScheduled) -----
    add(w3_id, AgentCreated, 53.5, role="pending", parent_id=mgr2_id, config=heuristic_config, sibling_index=0, depends_on=[])
    add(w3_id, TaskAssigned, 54, task_description="Add deserialization filter to block InvokerTransformer and ChainedTransformer")
    add(w3_id, StatusChanged, 54.5, old_status="pending", new_status="analyzing")
    add(w3_id, TokensConsumed, 55, model="gpt-4o-mini", prompt_tokens=900, completion_tokens=350, total_tokens=1250, cost_usd=0.0007, operation="complexity_evaluation")
    add(w3_id, ComplexityEvaluated, 55.5, complexity="low", determined_role="worker", reasoning="Single file modification")
    add(w3_id, PromptSent, 55.8, prompt="<ROLE>You are a Java security engineer.</ROLE>\n<TASK>Add deserialization filter to block InvokerTransformer and ChainedTransformer</TASK>\n<sibling-context>Sibling 'Compile and verify patch' depends on your output.</sibling-context>\n<shared-decisions>Root cause: InvokerTransformer gadget chain (from sibling analysis).</shared-decisions>\n<INSTRUCTIONS>Create a SafeObjectInputStream that blocks dangerous transformer classes during deserialization.</INSTRUCTIONS>", prompt_type="worker_execution", target="claude_code")
    add(w3_id, CodeGenerationStarted, 56, tool_name="claude_code")
    add(w3_id, StatusChanged, 56.5, old_status="analyzing", new_status="in_progress")
    add(w3_id, ThoughtCaptured, 57, content="Writing ObjectInputFilter to block dangerous transformer classes...", stream="tool", output_type="progress")
    add(w3_id, WorkerCostRecorded, 65, tool_name="claude_code", model="claude-sonnet-4-20250514", tokens=22000, cost_usd=0.066, duration_seconds=15.3)
    add(w3_id, OperationFinished, 65.5, operation_type="worker_execution", duration_seconds=15.3)
    add(w3_id, WorkCompleted, 66, result="Patch applied: SafeObjectInputStream with class allowlist")
    # Verification fails!
    add(w3_id, VerificationFailed, 67, failed_stage="execution", feedback="Unit test TestTransformerChain fails: expected InvokerTransformer to still work for internal uses", stages_passed=["structural", "deterministic"])
    add(w3_id, StatusChanged, 67.5, old_status="in_progress", new_status="failed")
    # Retry scheduled
    add(w3_id, RetryScheduled, 68, attempt=1, reason="Verification failed: unit test failure", escalated_model="claude-sonnet-4-20250514")
    add(w3_id, StatusChanged, 68.5, old_status="failed", new_status="analyzing")
    add(w3_id, CodeGenerationStarted, 69, tool_name="claude_code")
    add(w3_id, StatusChanged, 69.5, old_status="analyzing", new_status="in_progress")
    add(w3_id, ThoughtCaptured, 70, content="Refining patch: apply filter only at deserialization boundary, not at transformer instantiation.", stream="tool", output_type="thinking")
    add(w3_id, WorkerCostRecorded, 78, tool_name="claude_code", model="claude-sonnet-4-20250514", tokens=20000, cost_usd=0.06, duration_seconds=14.0)
    add(w3_id, OperationFinished, 78.5, operation_type="worker_execution", duration_seconds=14.0)
    add(w3_id, WorkCompleted, 79, result="Patch v2: Deserialization filter at boundary only. All tests pass.")
    add(w3_id, StatusChanged, 79.5, old_status="in_progress", new_status="completed")
    add(mgr2_id, ChildCompleted, 80, child_id=w3_id, result="Patch applied successfully on retry")

    # ----- WORKER 4 (build verification) -----
    add(w4_id, AgentCreated, 53.5, role="pending", parent_id=mgr2_id, config=heuristic_config, sibling_index=1, depends_on=[0])
    add(w4_id, TaskAssigned, 80.5, task_description="Compile patched code and run existing test suite")
    add(w4_id, StatusChanged, 81, old_status="pending", new_status="analyzing")
    add(w4_id, TokensConsumed, 81.5, model="gpt-4o-mini", prompt_tokens=800, completion_tokens=300, total_tokens=1100, cost_usd=0.0006, operation="complexity_evaluation")
    add(w4_id, ComplexityEvaluated, 82, complexity="low", determined_role="worker", reasoning="Build and test execution")
    add(w4_id, CodeGenerationStarted, 83, tool_name="openhands")
    add(w4_id, StatusChanged, 83.5, old_status="analyzing", new_status="in_progress")
    add(w4_id, ThoughtCaptured, 84, content="Running: mvn clean compile test", stream="tool", output_type="progress")
    add(w4_id, ThoughtCaptured, 95, content="BUILD SUCCESS\n148 tests run, 0 failures, 0 errors", stream="tool", output_type="output")
    add(w4_id, WorkerCostRecorded, 96, tool_name="openhands", model="gpt-4o", tokens=12000, cost_usd=0.036, duration_seconds=18.0)
    add(w4_id, OperationFinished, 96.5, operation_type="worker_execution", duration_seconds=18.0)
    add(w4_id, WorkCompleted, 97, result="Build successful. All 148 tests pass with patch applied.")
    add(w4_id, StatusChanged, 97.5, old_status="in_progress", new_status="completed")
    add(mgr2_id, ChildCompleted, 98, child_id=w4_id, result="Build and tests pass")
    add(mgr2_id, WorkCompleted, 99, result="Patch generated, applied, and verified. Build passes all 148 tests.")
    add(mgr2_id, StatusChanged, 99.5, old_status="waiting", new_status="completed")
    add(boss_id, ChildCompleted, 100, child_id=mgr2_id, result="Patch generation complete")

    # ----- MANAGER 3: Verification -----
    add(mgr3_id, AgentCreated, 100.5, role="pending", parent_id=boss_id, config=heuristic_config, sibling_index=2, depends_on=[1])
    add(mgr3_id, TaskAssigned, 101, task_description="Verify patch correctness: run security tests and regression suite")
    add(mgr3_id, StatusChanged, 101.5, old_status="pending", new_status="analyzing")
    add(mgr3_id, TokensConsumed, 102, model="gpt-4o-mini", prompt_tokens=1900, completion_tokens=800, total_tokens=2700, cost_usd=0.0016, operation="complexity_evaluation")
    add(mgr3_id, ComplexityEvaluated, 103, complexity="medium", determined_role="manager", reasoning="Multiple verification aspects")
    add(mgr3_id, TokensConsumed, 104, model="gpt-4o-mini", prompt_tokens=2600, completion_tokens=1000, total_tokens=3600, cost_usd=0.0022, operation="task_decomposition")
    add(mgr3_id, SubtasksDefined, 105, subtasks=[
        subtask("Run exploit PoC against patched code", 0),
        subtask("Run full regression test suite", 1),
    ])
    add(mgr3_id, StatusChanged, 106, old_status="analyzing", new_status="waiting")
    add(mgr3_id, ChildSpawned, 106.5, child_id=w5_id, child_role="pending", subtask=subtask("Run exploit PoC against patched code", 0), child_config=heuristic_config)
    add(mgr3_id, ChildSpawned, 107, child_id=w6_id, child_role="pending", subtask=subtask("Run full regression test suite", 1), child_config=heuristic_config)

    # ----- WORKER 5 (exploit PoC test) -----
    add(w5_id, AgentCreated, 107.5, role="pending", parent_id=mgr3_id, config=heuristic_config, sibling_index=0, depends_on=[])
    add(w5_id, TaskAssigned, 108, task_description="Run exploit PoC against patched code")
    add(w5_id, StatusChanged, 108.5, old_status="pending", new_status="analyzing")
    add(w5_id, TokensConsumed, 109, model="gpt-4o-mini", prompt_tokens=850, completion_tokens=350, total_tokens=1200, cost_usd=0.0007, operation="complexity_evaluation")
    add(w5_id, ComplexityEvaluated, 109.5, complexity="low", determined_role="worker", reasoning="Run PoC script")
    add(w5_id, PromptSent, 109.8, prompt="<ROLE>You are a penetration tester.</ROLE>\n<TASK>Run exploit PoC against patched code</TASK>\n<INSTRUCTIONS>Execute the CVE-2024-12345 proof-of-concept exploit against the patched codebase. The exploit attempts to deserialize a malicious InvokerTransformer payload. Verify the deserialization filter blocks it.</INSTRUCTIONS>", prompt_type="worker_execution", target="claude_code")
    add(w5_id, CodeGenerationStarted, 110, tool_name="claude_code")
    add(w5_id, StatusChanged, 110.5, old_status="analyzing", new_status="in_progress")
    add(w5_id, ThoughtCaptured, 111, content="Running PoC exploit: deserializing malicious InvokerTransformer payload...", stream="tool", output_type="progress")
    add(w5_id, ThoughtCaptured, 115, content="PoC BLOCKED: java.io.InvalidClassException - InvokerTransformer rejected by deserialization filter.", stream="tool", output_type="output")
    add(w5_id, WorkerCostRecorded, 116, tool_name="claude_code", model="claude-sonnet-4-20250514", tokens=10000, cost_usd=0.03, duration_seconds=7.0)
    add(w5_id, OperationFinished, 116.5, operation_type="worker_execution", duration_seconds=7.0)
    add(w5_id, WorkCompleted, 117, result="Exploit PoC blocked by deserialization filter. CVE-2024-12345 is patched.")
    add(w5_id, StatusChanged, 117.5, old_status="in_progress", new_status="completed")
    add(mgr3_id, ChildCompleted, 118, child_id=w5_id, result="Exploit blocked")

    # ----- WORKER 6 (regression suite) -----
    add(w6_id, AgentCreated, 107.5, role="pending", parent_id=mgr3_id, config=heuristic_config, sibling_index=1, depends_on=[])
    add(w6_id, TaskAssigned, 118.5, task_description="Run full regression test suite on patched codebase")
    add(w6_id, StatusChanged, 119, old_status="pending", new_status="analyzing")
    add(w6_id, TokensConsumed, 119.5, model="gpt-4o-mini", prompt_tokens=750, completion_tokens=300, total_tokens=1050, cost_usd=0.0006, operation="complexity_evaluation")
    add(w6_id, ComplexityEvaluated, 120, complexity="low", determined_role="worker", reasoning="Test execution")
    add(w6_id, CodeGenerationStarted, 121, tool_name="openhands")
    add(w6_id, StatusChanged, 121.5, old_status="analyzing", new_status="in_progress")
    add(w6_id, ThoughtCaptured, 122, content="Executing: mvn test -Dtest.groups=regression", stream="tool", output_type="progress")
    add(w6_id, ThoughtCaptured, 132, content="REGRESSION SUITE COMPLETE\n312 tests, 0 failures, 2 skipped\nCode coverage: 87%", stream="tool", output_type="output")
    add(w6_id, WorkerCostRecorded, 133, tool_name="openhands", model="gpt-4o", tokens=8000, cost_usd=0.024, duration_seconds=20.0)
    add(w6_id, OperationFinished, 133.5, operation_type="worker_execution", duration_seconds=20.0)
    add(w6_id, WorkCompleted, 134, result="Regression suite passed: 312/312 tests, 87% coverage.")
    add(w6_id, StatusChanged, 134.5, old_status="in_progress", new_status="completed")
    add(mgr3_id, ChildCompleted, 135, child_id=w6_id, result="Regression suite passed")
    add(mgr3_id, WorkCompleted, 136, result="All verification passed: exploit blocked, 312 regression tests pass.")
    add(mgr3_id, StatusChanged, 136.5, old_status="waiting", new_status="completed")
    add(boss_id, ChildCompleted, 137, child_id=mgr3_id, result="Verification complete")
    add(boss_id, WorkCompleted, 138, result="CVE-2024-12345 remediation complete: vulnerability identified, patched, and verified.")
    add(boss_id, StatusChanged, 138.5, old_status="waiting", new_status="completed")

    # =========================================================================
    # Insert via append_batch (uses proper serialization)
    # =========================================================================
    events_only = [e for e, _ in all_events]
    await store.append_batch(events_only, expected_version=-1)

    print(f"Seeded {len(events_only)} events for {len(seq)} agents")
    print(f"  BOSS: {boss_id}")
    print("  Hierarchy: 1 BOSS -> 3 MANAGERS -> 6 WORKERS")
    print("  Includes: VerificationFailed, RetryScheduled, ProbeStarted/Completed")
    print("  Dashboard: http://localhost:8000")

    await store.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
