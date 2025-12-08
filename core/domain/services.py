"""Domain services for the multi-agent system.

This module contains domain services - operations that don't naturally fit
on entities or value objects per Domain-Driven Design (Eric Evans).
"""

import json
import re

from pydantic import TypeAdapter, ValidationError

from core.domain.agent_config import AgentConfig
from core.domain.subtask import Subtask


# Regex pattern to extract JSON from markdown code blocks
# Matches: ```json ... ``` or ``` ... ``` (with optional language identifier)
MARKDOWN_CODE_BLOCK_PATTERN = re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def strip_markdown_code_block(text: str) -> str:
    """Strip markdown code block wrapper from LLM response.

    LLMs often wrap JSON responses in markdown code blocks like:
        ```json
        {"key": "value"}
        ```

    This function extracts the inner content.

    Args:
        text: Raw LLM response that may be wrapped in markdown.

    Returns:
        Inner content if wrapped in code block, otherwise original text.
    """
    match = MARKDOWN_CODE_BLOCK_PATTERN.match(text.strip())
    if match:
        return match.group(1).strip()
    return text.strip()


class SubtaskParser:
    """Domain service for parsing LLM responses into subtask domain objects.

    Responsibility: Transform external LLM JSON responses into validated
    Subtask domain objects, enforcing MANAGER agent decomposition invariants.

    Domain Invariants:
        1. MANAGER agents MUST decompose tasks into a list of subtasks
        2. Each subtask MUST include a complete, valid configuration for the child agent
        3. Parent's config decisions are validated before child creation

    Type Safety:
        Uses Pydantic TypeAdapter to validate AgentConfig structures, ensuring
        parents cannot spawn children with invalid configurations.
    """

    @staticmethod
    def parse_from_llm_response(response: str) -> list[Subtask]:
        """Parse LLM JSON response into validated list of Subtasks.

        This method enforces domain invariants:
        1. MANAGER must produce list of subtasks (decomposition responsibility)
        2. Each subtask must have description + config (parent config responsibility)
        3. Config must be valid AgentConfig (type safety)

        Args:
            response: Raw string response from LLM (expected to be JSON array).
                     Each item must have "description" and "config" fields.

        Returns:
            List of validated Subtask value objects with type-safe configs.

        Raises:
            ValueError: If response violates domain invariants:
                - Not valid JSON
                - Not a list/array
                - Empty list
                - Items missing 'description' or 'config'
                - Config is invalid (missing fields, wrong types, etc.)

        Example:
            >>> response = '''[
            ...   {
            ...     "description": "Research docs",
            ...     "config": {
            ...       "strategy": "heuristic",
            ...       "base": {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": 500},
            ...       "tool": "claude_code"
            ...     }
            ...   }
            ... ]'''
            >>> subtasks = SubtaskParser.parse_from_llm_response(response)
            >>> len(subtasks)
            1
        """
        # Strip markdown code blocks if present (LLMs often wrap JSON in ```json...```)
        clean_response = strip_markdown_code_block(response)

        # Parse JSON - raises ValueError if invalid
        try:
            data = json.loads(clean_response)
        except json.JSONDecodeError as e:
            msg = f"LLM response is not valid JSON: {e}"
            raise ValueError(msg) from e

        # Domain Invariant: MANAGER must produce LIST of subtasks
        if not isinstance(data, list):
            msg = (
                f"MANAGER failed to decompose task: expected list of subtasks, "
                f"got {type(data).__name__}"
            )
            raise ValueError(msg)

        # Domain Invariant: MANAGER must produce AT LEAST ONE subtask
        if len(data) == 0:
            msg = "MANAGER failed to decompose task: returned empty subtask list"
            raise ValueError(msg)

        # Validate and convert each item to Subtask value object
        config_adapter = TypeAdapter(AgentConfig)
        subtasks: list[Subtask] = []

        for idx, item in enumerate(data):
            # Domain Invariant: Each subtask must be a dict/object
            if not isinstance(item, dict):
                msg = (
                    f"Subtask {idx} has invalid structure: expected dict/object, "
                    f"got {type(item).__name__}"
                )
                raise ValueError(msg)

            # Domain Invariant: Each subtask must have 'config' field
            if "config" not in item:
                msg = (
                    f"Subtask {idx} missing required 'config' field "
                    f"(parent must specify child config)"
                )
                raise ValueError(msg)

            # Validate config structure using TypeAdapter
            # This ensures parent specified a complete, valid AgentConfig
            try:
                config_adapter.validate_python(item["config"])
            except ValidationError as e:
                msg = (
                    f"Subtask {idx} has invalid config: {e}. "
                    f"Parent must specify valid AgentConfig (strategy, models, hyperparameters)."
                )
                raise ValueError(msg) from e

            # Convert to Subtask value object (Pydantic validates description + config)
            try:
                subtask = Subtask(**item)
                subtasks.append(subtask)
            except ValidationError as e:
                # Pydantic validation failed (missing/invalid description or config)
                msg = f"Subtask {idx} validation failed: {e}"
                raise ValueError(msg) from e

        return subtasks
