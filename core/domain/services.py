"""Domain services for the multi-agent system.

This module contains domain services - operations that don't naturally fit
on entities or value objects per Domain-Driven Design (Eric Evans).
"""

import json

from pydantic import ValidationError

from core.domain.subtask import Subtask


class SubtaskParser:
    """Domain service for parsing LLM responses into subtask domain objects.

    Responsibility: Transform external LLM JSON responses into validated
    Subtask domain objects, enforcing MANAGER agent decomposition invariants.

    Domain Invariant:
        MANAGER agents MUST decompose tasks into a list of subtasks.
        Any other response format indicates failure to fulfill core responsibility.
    """

    @staticmethod
    def parse_from_llm_response(response: str) -> list[Subtask]:
        """Parse LLM JSON response into validated list of Subtasks.

        This method enforces the domain invariant that MANAGER agents must
        produce a list of subtasks. Failures indicate the MANAGER failed its
        core responsibility (decomposition), not just technical errors.

        Args:
            response: Raw string response from LLM (expected to be JSON array).

        Returns:
            List of validated Subtask value objects.

        Raises:
            ValueError: If response violates domain invariants:
                - Not valid JSON
                - Not a list/array
                - Empty list
                - Items not dicts with 'description'
                - Description empty or missing

        Example:
            >>> response = '[{"description": "Task 1"}, {"description": "Task 2"}]'
            >>> subtasks = SubtaskParser.parse_from_llm_response(response)
            >>> len(subtasks)
            2
        """
        # Parse JSON - raises ValueError if invalid
        try:
            data = json.loads(response)
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
        subtasks: list[Subtask] = []
        for idx, item in enumerate(data):
            # Domain Invariant: Each subtask must be a dict/object
            if not isinstance(item, dict):
                msg = (
                    f"Subtask {idx} has invalid structure: expected dict/object, "
                    f"got {type(item).__name__}"
                )
                raise ValueError(msg)

            # Convert to Subtask value object (Pydantic validates)
            try:
                subtask = Subtask(**item)
                subtasks.append(subtask)
            except ValidationError as e:
                # Pydantic validation failed (missing/invalid description)
                msg = f"Subtask {idx} validation failed: {e}"
                raise ValueError(msg) from e

        return subtasks
