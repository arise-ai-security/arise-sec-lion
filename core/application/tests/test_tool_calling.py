"""Integration tests for the manager tool-calling reconnaissance system.

Tests the full loop: ToolCallingService → mock LLMPort → mock toolset,
verifying that tool calls are dispatched, results accumulated, and probe
events emitted correctly through the orchestrator.
"""

import json
import pytest

from core.application.services.toolset_context import ActiveToolContext, LoopPolicy
from core.application.services.tool_calling_service import (
    ToolCallingService,
    ToolCallingResult,
)
from core.domain.values.llm_response import (
    LLMResponse,
    LLMToolResponse,
    LLMUsage,
    ToolCall,
)


# ---------------------------------------------------------------------------
# Mock ports
# ---------------------------------------------------------------------------


class MockLLMPort:
    """Mock LLM that returns scripted responses.

    Accepts a list of LLMToolResponse objects to return in sequence.
    """

    def __init__(self, responses: list[LLMToolResponse]) -> None:
        self._responses = list(responses)
        self._call_index = 0
        self.calls: list[dict] = []

    async def query(self, prompt, config_dict):
        raise NotImplementedError

    async def query_with_usage(self, prompt, config_dict):
        raise NotImplementedError

    async def query_with_tools(self, messages, config_dict, tools):
        self.calls.append({
            "messages": messages,
            "config_dict": config_dict,
            "tools": tools,
        })
        response = self._responses[self._call_index]
        self._call_index += 1
        return response


class MockReconToolPort:
    """Mock recon tool that records calls and returns canned results."""

    def __init__(self, results: dict[str, str] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return "recon"

    async def read_file(self, path, max_lines=200):
        return self._results.get(f"read_file:{path}", f"contents of {path}")

    async def list_directory(self, path):
        return self._results.get(f"list_directory:{path}", "file1.py\nfile2.py")

    async def search_codebase(self, pattern, path="."):
        return self._results.get(f"search_codebase:{pattern}", f"match: {pattern}")

    async def find_file(self, pattern, path="."):
        return self._results.get(f"find_file:{pattern}", f"found: {pattern}")

    async def get_file_structure(self, path=".", max_depth=3):
        return self._results.get("get_file_structure", "├── src/\n└── tests/")

    def get_tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_directory",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                },
            },
        ]

    async def execute_tool(self, name, arguments):
        self.calls.append((name, arguments))
        dispatch = {
            "read_file": self.read_file,
            "list_directory": self.list_directory,
            "search_codebase": self.search_codebase,
            "find_file": self.find_file,
            "get_file_structure": self.get_file_structure,
        }
        handler = dispatch.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown tool: {name}"})
        return await handler(**arguments)


def _usage(p=10, c=5) -> LLMUsage:
    return LLMUsage(prompt_tokens=p, completion_tokens=c, total_tokens=p + c)


def _tool_context(
    toolset: MockReconToolPort,
    *,
    max_iterations: int = 5,
    result_char_limit: int = 6_000,
) -> ActiveToolContext:
    tool_definitions = tuple(toolset.get_tool_definitions())
    executors = {
        tool_def["function"]["name"]: toolset
        for tool_def in tool_definitions
        if isinstance(tool_def.get("function", {}).get("name"), str)
    }
    return ActiveToolContext(
        tool_definitions=tool_definitions,
        loop_policy=LoopPolicy(
            max_iterations=max_iterations,
            result_char_limit=result_char_limit,
        ),
        executors=executors,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestToolCallingService:
    """Tests for the agentic tool-calling loop."""

    @pytest.mark.asyncio
    async def test_direct_text_response_no_tool_calls(self):
        """LLM responds immediately without tool calls → single turn."""
        llm = MockLLMPort([
            LLMToolResponse(
                content='{"action": "execute", "reasoning": "simple task"}',
                tool_calls=[],
                usage=_usage(),
                model="gpt-4",
                cost_usd=0.001,
            ),
        ])
        recon = MockReconToolPort()
        service = ToolCallingService(llm, max_iterations=5)

        result = await service.run_with_tools(
            "assess this task",
            {"model": "gpt-4"},
            tool_context=_tool_context(recon),
        )

        assert isinstance(result, ToolCallingResult)
        assert result.response.content == '{"action": "execute", "reasoning": "simple task"}'
        assert result.tool_records == []
        assert len(llm.calls) == 1
        assert len(recon.calls) == 0

    @pytest.mark.asyncio
    async def test_single_tool_call_then_final_answer(self):
        """LLM calls one tool, then responds with final answer."""
        llm = MockLLMPort([
            # Turn 1: LLM wants to read a file
            LLMToolResponse(
                content=None,
                tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "main.py"})],
                usage=_usage(20, 10),
                model="gpt-4",
                cost_usd=0.002,
            ),
            # Turn 2: LLM gives final answer
            LLMToolResponse(
                content='{"action": "execute", "reasoning": "read main.py, straightforward"}',
                tool_calls=[],
                usage=_usage(30, 15),
                model="gpt-4",
                cost_usd=0.003,
            ),
        ])
        recon = MockReconToolPort({"read_file:main.py": "print('hello')"})
        service = ToolCallingService(llm, max_iterations=5)

        result = await service.run_with_tools(
            "assess task",
            {"model": "gpt-4"},
            tool_context=_tool_context(recon),
        )

        # Verify final response
        assert "execute" in result.response.content
        # Verify aggregated usage
        assert result.response.usage.prompt_tokens == 50  # 20 + 30
        assert result.response.usage.completion_tokens == 25  # 10 + 15
        assert result.response.cost_usd == 0.005
        # Verify tool records
        assert len(result.tool_records) == 1
        assert result.tool_records[0].tool_name == "read_file"
        assert result.tool_records[0].arguments == {"path": "main.py"}
        assert result.tool_records[0].result_summary == "print('hello')"
        # Verify recon was called
        assert len(recon.calls) == 1
        assert recon.calls[0] == ("read_file", {"path": "main.py"})
        # Verify LLM received tool results in conversation
        assert len(llm.calls) == 2
        second_call_messages = llm.calls[1]["messages"]
        # Should have: user, assistant (with tool_calls), tool result
        assert second_call_messages[0]["role"] == "user"
        assert second_call_messages[1]["role"] == "assistant"
        assert second_call_messages[2]["role"] == "tool"
        assert second_call_messages[2]["content"] == "print('hello')"

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_per_turn(self):
        """LLM requests multiple tool calls in a single turn."""
        llm = MockLLMPort([
            # Turn 1: LLM wants to read a file AND list a directory
            LLMToolResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"}),
                    ToolCall(id="call_2", name="list_directory", arguments={"path": "src"}),
                ],
                usage=_usage(),
                model="gpt-4",
                cost_usd=0.001,
            ),
            # Turn 2: final answer
            LLMToolResponse(
                content='{"action": "decompose", "reasoning": "complex"}',
                tool_calls=[],
                usage=_usage(),
                model="gpt-4",
                cost_usd=0.001,
            ),
        ])
        recon = MockReconToolPort()
        service = ToolCallingService(llm, max_iterations=5)

        result = await service.run_with_tools(
            "assess",
            {"model": "gpt-4"},
            tool_context=_tool_context(recon),
        )

        assert len(result.tool_records) == 2
        assert result.tool_records[0].tool_name == "read_file"
        assert result.tool_records[1].tool_name == "list_directory"
        assert len(recon.calls) == 2
        # Verify both tool results sent back to LLM
        second_messages = llm.calls[1]["messages"]
        tool_messages = [m for m in second_messages if m["role"] == "tool"]
        assert len(tool_messages) == 2

    @pytest.mark.asyncio
    async def test_max_iterations_forces_final_answer(self):
        """When max_iterations is hit, service forces LLM to give text response."""
        # LLM always requests tools — never stops voluntarily
        tool_response = LLMToolResponse(
            content=None,
            tool_calls=[ToolCall(id="call_x", name="read_file", arguments={"path": "x.py"})],
            usage=_usage(),
            model="gpt-4",
            cost_usd=0.001,
        )
        final_response = LLMToolResponse(
            content='{"action": "execute", "reasoning": "forced"}',
            tool_calls=[],
            usage=_usage(),
            model="gpt-4",
            cost_usd=0.001,
        )
        # 3 tool-calling turns + 1 forced final
        llm = MockLLMPort([tool_response, tool_response, tool_response, final_response])
        recon = MockReconToolPort()
        service = ToolCallingService(llm, max_iterations=3)

        result = await service.run_with_tools(
            "assess",
            {"model": "gpt-4"},
            tool_context=_tool_context(recon, max_iterations=3),
        )

        assert "forced" in result.response.content
        assert len(result.tool_records) == 3
        # The 4th call should have empty tools list (forcing text)
        assert llm.calls[3]["tools"] == []
        # The forced prompt should be in the messages
        last_messages = llm.calls[3]["messages"]
        user_msgs = [m for m in last_messages if m["role"] == "user"]
        assert any("maximum number of tool calls" in m["content"] for m in user_msgs)

    @pytest.mark.asyncio
    async def test_usage_aggregated_across_turns(self):
        """Token usage and cost are accumulated across all turns."""
        llm = MockLLMPort([
            LLMToolResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})],
                usage=LLMUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                model="gpt-4",
                cost_usd=0.01,
            ),
            LLMToolResponse(
                content='{"action": "execute"}',
                tool_calls=[],
                usage=LLMUsage(prompt_tokens=200, completion_tokens=100, total_tokens=300),
                model="gpt-4",
                cost_usd=0.02,
            ),
        ])
        recon = MockReconToolPort()
        service = ToolCallingService(llm)

        result = await service.run_with_tools(
            "assess",
            {"model": "gpt-4"},
            tool_context=_tool_context(recon),
        )

        assert result.response.usage.prompt_tokens == 300
        assert result.response.usage.completion_tokens == 150
        assert result.response.usage.total_tokens == 450
        assert result.response.cost_usd == 0.03

    @pytest.mark.asyncio
    async def test_result_summary_truncated_at_500_chars(self):
        """Tool results longer than 500 chars are truncated in records."""
        long_result = "x" * 1000
        recon = MockReconToolPort({"read_file:big.py": long_result})
        llm = MockLLMPort([
            LLMToolResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "big.py"})],
                usage=_usage(),
                model="gpt-4",
                cost_usd=0.001,
            ),
            LLMToolResponse(
                content='{"action": "execute"}',
                tool_calls=[],
                usage=_usage(),
                model="gpt-4",
                cost_usd=0.001,
            ),
        ])
        service = ToolCallingService(llm)

        result = await service.run_with_tools(
            "assess",
            {"model": "gpt-4"},
            tool_context=_tool_context(recon),
        )

        assert len(result.tool_records[0].result_summary) == 500
        # But the full result was sent to the LLM
        tool_msg = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]
        assert len(tool_msg["content"]) == 1000


class TestToolsetPolicyResolver:
    """Tests for role/domain toolset resolution."""

    def test_domain_override_filters_tools(self):
        from core.application.services.toolset_policy_resolver import ToolsetPolicyResolver
        from core.domain.values.enums import AgentRole

        recon = MockReconToolPort()
        resolver = ToolsetPolicyResolver(
            toolsets=[recon],
            config={
                "default": {
                    "manager": {
                        "max_iterations": 5,
                        "result_char_limit": 6_000,
                        "toolsets": {
                            "recon": {"enabled": True},
                        },
                    },
                },
                "domains": {
                    "secbench": {
                        "manager": {
                            "max_iterations": 3,
                            "toolsets": {
                                "recon": {"allowed_tools": ["read_file"]},
                            },
                        },
                    },
                },
            },
        )

        resolved = resolver.resolve(AgentRole.MANAGER, "secbench")

        assert resolved.loop_policy.max_iterations == 3
        assert [tool["function"]["name"] for tool in resolved.tool_definitions] == ["read_file"]
        assert set(resolved.executors) == {"read_file"}


class TestReconToolAdapter:
    """Tests for the actual filesystem adapter."""

    @pytest.mark.asyncio
    async def test_read_file_real(self, tmp_path):
        """ReconToolAdapter can read a real file."""
        from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter

        (tmp_path / "test.txt").write_text("line1\nline2\nline3")
        adapter = ReconToolAdapter(str(tmp_path))

        result = await adapter.read_file("test.txt")
        assert "line1" in result
        assert "line3" in result

    @pytest.mark.asyncio
    async def test_list_directory_real(self, tmp_path):
        from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter

        (tmp_path / "alpha.py").touch()
        (tmp_path / "beta.py").touch()
        (tmp_path / "subdir").mkdir()
        adapter = ReconToolAdapter(str(tmp_path))

        result = await adapter.list_directory(".")
        assert "alpha.py" in result
        assert "beta.py" in result
        assert "subdir/" in result

    @pytest.mark.asyncio
    async def test_path_escape_prevented(self, tmp_path):
        from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter

        adapter = ReconToolAdapter(str(tmp_path))

        with pytest.raises(ValueError, match="escapes working directory"):
            await adapter.read_file("../../etc/passwd")

    @pytest.mark.asyncio
    async def test_find_file_real(self, tmp_path):
        from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").touch()
        (tmp_path / "src" / "utils.py").touch()
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_app.py").touch()
        adapter = ReconToolAdapter(str(tmp_path))

        result = await adapter.find_file("*.py")
        assert "app.py" in result
        assert "utils.py" in result
        assert "test_app.py" in result

    @pytest.mark.asyncio
    async def test_get_file_structure_real(self, tmp_path):
        from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()
        adapter = ReconToolAdapter(str(tmp_path))

        result = await adapter.get_file_structure(".")
        assert "src/" in result
        assert "main.py" in result

    @pytest.mark.asyncio
    async def test_execute_tool_dispatches(self, tmp_path):
        from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter

        (tmp_path / "hello.txt").write_text("hello world")
        adapter = ReconToolAdapter(str(tmp_path))

        result = await adapter.execute_tool("read_file", {"path": "hello.txt"})
        assert "hello world" in result

    @pytest.mark.asyncio
    async def test_execute_tool_unknown(self, tmp_path):
        from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter

        adapter = ReconToolAdapter(str(tmp_path))
        result = await adapter.execute_tool("delete_everything", {})
        assert "Unknown tool" in result

    @pytest.mark.asyncio
    async def test_tool_definitions_format(self, tmp_path):
        from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter

        adapter = ReconToolAdapter(str(tmp_path))
        defs = adapter.get_tool_definitions()

        assert len(defs) == 7  # 5 original + get_symbols_overview + read_symbol
        for d in defs:
            assert d["type"] == "function"
            assert "name" in d["function"]
            assert "parameters" in d["function"]


class TestLLMUsageAddition:
    """Tests for LLMUsage.__add__."""

    def test_add_two_usages(self):
        a = LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        b = LLMUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30)
        c = a + b
        assert c.prompt_tokens == 30
        assert c.completion_tokens == 15
        assert c.total_tokens == 45

    def test_add_preserves_immutability(self):
        a = LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        b = LLMUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30)
        c = a + b
        # Originals unchanged
        assert a.prompt_tokens == 10
        assert b.prompt_tokens == 20
        assert c.prompt_tokens == 30
