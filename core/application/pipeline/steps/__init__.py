"""Pipeline steps for agent orchestration.

Each step is a small, focused, independently testable unit that:
- Receives PipelineContext
- Performs one logical operation
- Returns StepResult (success with updated context, or failure with reason)

Steps are organized by responsibility:
- validation: Agent state validation
- observability: Event emission for monitoring
- prompt: Prompt construction
- llm: LLM interactions
- parsing: Response parsing
- deduplication: Task registry operations
- limits: Limit enforcement
- domain: Domain method invocations
- worker: Worker tool execution
"""
