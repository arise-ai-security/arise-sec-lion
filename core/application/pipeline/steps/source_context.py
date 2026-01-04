"""Source context extraction and injection steps (Design Choice 6 & 7).

This module provides steps for:
1. Extracting structured key information from boss task description (DC6)
2. Inferring CWE patterns and fix strategies (DC7)
3. Publishing to SharedExecutionContext
4. Injecting into descendant prompts

Design Choice 6 & 7 Flow:
  BOSS created → ExtractSourceContext → LLM extracts structured info
  → PublishSourceContext → SourceContextExtracted event
  → Descendants → InjectSourceContext → reads from SharedExecutionContext
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from core.application.pipeline.context import PipelineState, StepResult
from core.application.services.context_composer import ContextComposer
from core.application.services.context_factories import source_context_from_stored
from core.domain.values.context import SourceContext
from core.domain.values.enums import AgentRole

if TYPE_CHECKING:
    from core.application.services.prompt_builder import PromptBuilder
    from core.ports.llm_port import LLMPort
    from core.ports.shared_context_port import SharedContextPort

logger = logging.getLogger(__name__)


# =============================================================================
# Extraction Step (BOSS only)
# =============================================================================


class ExtractSourceContext:
    """Extract structured key information from boss task description (DC6 & DC7).

    Uses LLM to parse unstructured user input and extract:
    - Bug summary, error messages, reproduction steps (DC6)
    - Referenced files, commits, URLs
    - Environment and dependencies
    - Inferred CWE patterns and fix strategies (DC7)

    This step should run at the beginning of the BOSS decomposition pipeline,
    before task decomposition, so extracted context is available to all children.

    Only runs for BOSS agents; skips for MANAGER and WORKER.
    """

    def __init__(
        self,
        llm_port: "LLMPort",
        prompt_builder: "PromptBuilder",
    ) -> None:
        """Initialize with LLM port and prompt builder.

        Args:
            llm_port: Port for LLM interactions (extraction call).
            prompt_builder: Builder for constructing extraction prompt.
        """
        self._llm_port = llm_port
        self._prompt_builder = prompt_builder

    async def execute(self, state: PipelineState) -> StepResult:
        """Extract source context from boss task description.

        Creates SourceContext and attaches it to pipeline state for
        subsequent publishing step.
        """
        agent = state.agent

        # Only extract for BOSS agents (not MANAGER)
        if agent.role != AgentRole.BOSS:
            return StepResult.ok(state)

        task_description = agent.task_description or ""
        if not task_description:
            return StepResult.ok(state)

        # Build extraction prompt
        prompt = self._prompt_builder.build_source_context_extraction_prompt(
            task_description=task_description,
        )

        # Call LLM for extraction
        try:
            response = await self._llm_port.query_with_usage(
                prompt=prompt,
                config_dict={
                    "max_tokens": 2000,
                    "temperature": 0.0,
                },
            )

            # Parse JSON response
            extracted = self._parse_extraction_response(response.content)

            # Create SourceContext
            source_context = SourceContext.from_extraction_event(
                extraction_summary=extracted.get("extraction_summary", ""),
                key_references=tuple(extracted.get("key_references", [])),
                has_bug_report=extracted.get("has_bug_report", False),
                has_error_details=extracted.get("has_error_details", False),
                has_file_references=extracted.get("has_file_references", False),
                has_code_snippets=extracted.get("has_code_snippets", False),
                extracted_entities=extracted.get("extracted_entities", {}),
                inferred_cwes=tuple(extracted.get("inferred_cwes", [])),
                cwe_reasoning=extracted.get("cwe_reasoning", {}),
                recommended_sanitizers=tuple(extracted.get("recommended_sanitizers", [])),
                fix_patterns=extracted.get("fix_patterns", {}),
            )

            # Add to context composer for publishing step
            composer = state.context_composer or ContextComposer()
            composer.add(source_context)

            return StepResult.ok(state.with_context_composer(composer))

        except Exception as e:
            logger.warning(f"Source context extraction failed: {e}")
            # Non-fatal: continue pipeline without extraction
            return StepResult.ok(state)

    def _parse_extraction_response(self, content: str) -> dict[str, Any]:
        """Parse LLM response to extract JSON data.

        Handles cases where response contains markdown code blocks.
        """
        # Try to find JSON in response
        content = content.strip()

        # Remove markdown code block if present
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Try to extract JSON object from response
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(content[start:end])
                except json.JSONDecodeError:
                    pass

            logger.warning("Could not parse extraction response as JSON")
            return {}


# =============================================================================
# Publishing Step (BOSS only)
# =============================================================================


class PublishSourceContext:
    """Publish extracted source context to SharedExecutionContext (DC6 & DC7).

    Takes the SourceContext created by ExtractSourceContext and publishes
    it to the SharedExecutionContext so all descendant agents can access it.

    This step should run after ExtractSourceContext and before task decomposition.
    Only runs for BOSS agents.
    """

    def __init__(self, shared_context_port: "SharedContextPort") -> None:
        """Initialize with shared context port.

        Args:
            shared_context_port: Port for accessing shared execution context.
        """
        self._shared_context_port = shared_context_port

    async def execute(self, state: PipelineState) -> StepResult:
        """Publish source context to shared execution context.

        Reads SourceContext from pipeline state context composer and
        publishes it to SharedExecutionContext.
        """
        agent = state.agent

        # Only publish for BOSS agents
        if agent.role != AgentRole.BOSS:
            return StepResult.ok(state)

        # Get source context from composer
        if state.context_composer is None:
            return StepResult.ok(state)

        if not state.context_composer.has("source_context"):
            return StepResult.ok(state)

        ctx = state.context_composer.build()
        source_ctx_dict = ctx.get("source_context", {})
        if not source_ctx_dict:
            return StepResult.ok(state)

        # Get or create shared context
        root_id = state.root_id
        if root_id is None:
            return StepResult.ok(state)

        shared_context = await self._shared_context_port.get(root_id)
        if shared_context is None:
            return StepResult.ok(state)

        # Publish source context
        current_version = shared_context.version
        shared_context.publish_source_context(
            extraction_summary=source_ctx_dict.get("extraction_summary", ""),
            key_references=list(source_ctx_dict.get("key_references", [])),
            has_bug_report=source_ctx_dict.get("has_bug_report", False),
            has_error_details=source_ctx_dict.get("has_error_details", False),
            has_file_references=source_ctx_dict.get("has_file_references", False),
            has_code_snippets=source_ctx_dict.get("has_code_snippets", False),
            extracted_entities=source_ctx_dict.get("extracted_entities", {}),
            inferred_cwes=list(source_ctx_dict.get("inferred_cwes", [])),
            cwe_reasoning=source_ctx_dict.get("cwe_reasoning", {}),
            recommended_sanitizers=list(source_ctx_dict.get("recommended_sanitizers", [])),
            fix_patterns=source_ctx_dict.get("fix_patterns", {}),
            extracted_by=agent.agent_id,
        )

        # Persist
        if shared_context.events:
            await self._shared_context_port.save(
                shared_context, expected_version=current_version
            )
            shared_context.mark_changes_as_committed()

        return StepResult.ok(state)


# =============================================================================
# Injection Step (Descendants)
# =============================================================================


class InjectSourceContext:
    """Inject source context into descendant prompts (DC6 & DC7).

    Fetches the SourceContext published by BOSS from SharedExecutionContext
    and injects it into the agent's prompt via ContextComposer.

    This enables all workers and managers to access the structured source
    information extracted from the original user input.

    This step should run before prompt building steps.
    Skips for BOSS agents (they extract, not inherit).
    """

    def __init__(self, shared_context_port: "SharedContextPort") -> None:
        """Initialize with shared context port.

        Args:
            shared_context_port: Port for accessing shared execution context.
        """
        self._shared_context_port = shared_context_port

    async def execute(self, state: PipelineState) -> StepResult:
        """Inject source context into pipeline state context composer.

        Fetches stored SourceContext from SharedExecutionContext and
        converts it to SourceContext for template rendering.
        """
        agent = state.agent

        # Skip for BOSS (extracts, doesn't inherit)
        if agent.role == AgentRole.BOSS:
            return StepResult.ok(state)

        root_id = state.root_id
        if root_id is None:
            return StepResult.ok(state)

        shared_context = await self._shared_context_port.get(root_id)
        if shared_context is None:
            return StepResult.ok(state)

        stored_context = shared_context.get_source_context()
        if stored_context is None:
            return StepResult.ok(state)

        # Convert to SourceContext for template rendering using factory
        source_context = source_context_from_stored(stored_context)

        # Add to context composer
        composer = state.context_composer or ContextComposer()
        composer.add(source_context)

        return StepResult.ok(state.with_context_composer(composer))
