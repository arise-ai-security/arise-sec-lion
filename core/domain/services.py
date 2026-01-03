"""Domain services: parsing and utility functions."""

import json
import re

from pydantic import TypeAdapter, ValidationError

from core.domain.agent_config import AgentConfig
from core.domain.subtask import Subtask


MARKDOWN_CODE_BLOCK_PATTERN = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?\s*```",
    re.DOTALL | re.IGNORECASE,
)

JSON_ARRAY_PATTERN = re.compile(
    r"\[\s*\{.*?\}\s*\]",
    re.DOTALL,
)


def strip_markdown_code_block(text: str) -> str:
    """Extract JSON content from LLM response, handling various formats.

    Handles:
    - Entire response wrapped in ```json...```
    - Code block with text before/after
    - Raw JSON array without code block
    """
    text = text.strip()

    # Try to find JSON code block anywhere in response
    match = MARKDOWN_CODE_BLOCK_PATTERN.search(text)
    if match:
        return match.group(1).strip()

    # Try to find raw JSON array pattern
    match = JSON_ARRAY_PATTERN.search(text)
    if match:
        return match.group(0)

    return text


class SubtaskParser:
    """Parse LLM JSON responses into validated Subtask objects."""

    @staticmethod
    def parse_from_llm_response(
        response: str,
        default_tool: str | None = None,
    ) -> list[Subtask]:
        """Parse JSON array into Subtasks. Validates config against AgentConfig schema.

        Args:
            response: LLM response containing JSON array of subtasks.
            default_tool: Default worker tool to use if not specified in config.
                         If None, falls back to AgentConfig's hardcoded default.
        """
        clean_response = strip_markdown_code_block(response)

        try:
            data = json.loads(clean_response)
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM response is not valid JSON: {e}") from e

        if not isinstance(data, list):
            raise ValueError(f"Expected list of subtasks, got {type(data).__name__}")

        if len(data) == 0:
            raise ValueError("Empty subtask list")

        config_adapter = TypeAdapter(AgentConfig)
        subtasks: list[Subtask] = []

        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"Subtask {idx}: expected dict, got {type(item).__name__}")

            if "config" not in item:
                raise ValueError(f"Subtask {idx}: missing 'config' field")

            # Inject default_tool if not specified in config
            config = item["config"]
            if default_tool and "tool" not in config:
                config["tool"] = default_tool

            try:
                config_adapter.validate_python(config)
            except ValidationError as e:
                raise ValueError(f"Subtask {idx}: invalid config: {e}") from e

            try:
                subtask = Subtask(**item)
                subtasks.append(subtask)
            except ValidationError as e:
                raise ValueError(f"Subtask {idx}: validation failed: {e}") from e

        return subtasks
