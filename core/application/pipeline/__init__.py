"""Pipeline infrastructure for AgentOrchestrator.

This module provides a composable Pipeline/Chain architecture for orchestrating
agent interactions with LLM and worker tools.

Key components:
- PipelineState: Immutable state passed through pipeline steps
- StepResult: Result of a pipeline step execution
- PipelineStep: Protocol for pipeline steps
- Pipeline: Executor that runs steps sequentially, short-circuiting on failure
"""

from core.application.pipeline.context import PipelineState, StepResult
from core.application.pipeline.executor import Pipeline
from core.application.pipeline.protocol import PipelineStep

__all__ = [
    "Pipeline",
    "PipelineState",
    "PipelineStep",
    "StepResult",
]
