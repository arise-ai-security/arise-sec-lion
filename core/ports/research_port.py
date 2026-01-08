"""Research port: read-only tool calling for RESEARCHER agents.

This port enables RESEARCHER agents to gather context via LLM tool calling
before transitioning to MANAGER for task decomposition.
"""

from dataclasses import dataclass
from typing import Any, Protocol

# Available research tools (contract between core and infrastructure)
RESEARCH_TOOL_NAMES = frozenset({"file_read", "grep_search", "list_files", "web_fetch"})
DEFAULT_RESEARCH_TOOLS = list(RESEARCH_TOOL_NAMES)


@dataclass(frozen=True)
class ResearchResult:
    """Result from a research session.

    Attributes:
        findings: Summary of research findings from the LLM
        tool_calls: List of tool calls made during research
        prompt_tokens: Total prompt tokens consumed
        completion_tokens: Total completion tokens consumed
    """

    findings: str
    tool_calls: list[dict[str, Any]]
    prompt_tokens: int
    completion_tokens: int


class ResearchPort(Protocol):
    """Execute read-only research via LLM tool calling.

    Provides context gathering capabilities for RESEARCHER agents using
    OpenAI-style function calling (supported by LiteLLM for all providers).

    Available tools:
    - file_read: Read file contents
    - grep_search: Search for patterns in files
    - list_files: List files matching patterns
    - web_fetch: Fetch content from URLs (optional)
    """

    async def run_research_session(
        self,
        task_description: str,
        working_directory: str,
        config_dict: dict[str, Any],
        available_tools: list[str] | None = None,
        max_tool_calls: int = 10,
    ) -> ResearchResult:
        """Execute research with LLM tool calling loop.

        Args:
            task_description: The task to research (becomes the user prompt)
            working_directory: Base directory for file operations
            config_dict: LLM configuration (model, temperature, etc.)
            available_tools: Tools to enable (default: all read-only tools)
            max_tool_calls: Maximum tool calls before forcing summary

        Returns:
            ResearchResult with findings, tool calls, and token counts

        Raises:
            LLMError: On LLM API failure
            ResearchError: On tool execution failure
        """
        ...
