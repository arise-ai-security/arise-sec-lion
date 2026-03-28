"""Encapsulate direct and tool-augmented LLM queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.application.services.toolset.tool_calling_service import ToolCallingService, ToolRecord
from core.domain.values.llm_response import LLMResponse


if TYPE_CHECKING:
    from core.application.services.toolset.toolset_context import ActiveToolContext
    from core.ports.runtime_ports import LLMPort


@dataclass(frozen=True, slots=True)
class LLMQueryResult:
    """Result of a single orchestrator-owned LLM query."""

    response: LLMResponse
    tool_records: list[ToolRecord] = field(default_factory=list)


class LLMQueryExecutor:
    """Route queries to raw LLM usage or the tool-calling loop."""

    def __init__(
        self,
        llm_port: LLMPort,
        tool_calling_service: ToolCallingService | None = None,
    ) -> None:
        self._llm_port = llm_port
        self._tool_calling_service = tool_calling_service

    async def query(
        self,
        prompt: str,
        config_dict: dict[str, Any],
        tool_context: ActiveToolContext | None = None,
    ) -> LLMQueryResult:
        """Execute the query with tools when a non-empty tool context is active."""
        if self._tool_calling_service is None or tool_context is None or not tool_context.has_tools:
            response = await self._llm_port.query_with_usage(prompt, config_dict)
            return LLMQueryResult(response=response)

        result = await self._tool_calling_service.run_with_tools(
            prompt,
            config_dict,
            tool_context=tool_context,
        )
        return LLMQueryResult(
            response=result.response,
            tool_records=result.tool_records,
        )
