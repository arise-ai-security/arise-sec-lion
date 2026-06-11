"""Integration tests for the manager tool-calling reconnaissance system.

Tests the full loop: ToolCallingService → mock LLMPort → mock toolset,
verifying that tool calls are dispatched, results accumulated, and probe
events emitted correctly through the orchestrator.
"""

import json

import pytest

from core.application.services import ActiveToolContext, LoopPolicy, ToolCallingService
from core.application.services.toolset.tool_calling_service import ToolCallingResult
from core.domain.values.llm_response import (
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


class FakeReconTool:
    """Fake toolset satisfying the Toolset protocol for filesystem adapter tests."""

    name = "recon"

    def get_tool_definitions(self):
        return [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }]

    async def execute_tool_call(self, name, arguments):
        return "fake result"


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
        service = ToolCallingService(llm)

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
        service = ToolCallingService(llm)

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
        service = ToolCallingService(llm)

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
        service = ToolCallingService(llm)

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
        from core.application.services import ToolsetPolicyResolver
        from core.domain.values.enums import AgentRole

        recon = MockReconToolPort()
        resolver = ToolsetPolicyResolver(
            toolsets=[recon],
            domain_key="test_domain",
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
                    "test_domain": {
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

        resolved = resolver.resolve(AgentRole.MANAGER)

        assert resolved.loop_policy.max_iterations == 3
        assert [tool["function"]["name"] for tool in resolved.tool_definitions] == ["read_file"]
        assert set(resolved.executors) == {"read_file"}


class TestFakeReconTool:
    """Tests for the FakeReconTool used in place of the real filesystem adapter."""

    def test_tool_definitions_format(self):
        # Given: a FakeReconTool instance
        adapter = FakeReconTool()

        # When: retrieving tool definitions
        defs = adapter.get_tool_definitions()

        # Then: definitions follow the expected schema
        assert len(defs) == 1
        assert defs[0]["type"] == "function"
        assert "name" in defs[0]["function"]
        assert "parameters" in defs[0]["function"]

    @pytest.mark.asyncio
    async def test_execute_tool_call_returns_fake_result(self):
        # Given: a FakeReconTool instance
        adapter = FakeReconTool()

        # When: executing a tool call
        result = await adapter.execute_tool_call("read_file", {"path": "any.py"})

        # Then: the fake result is returned
        assert result == "fake result"


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


class TestSourceReadCapture:
    """The loop captures FULL results for tools named in source_read_tools."""

    @pytest.mark.asyncio
    async def test_read_file_result_captured_verbatim(self):
        # Given: an LLM that calls read_file once then answers, and a context
        # that marks read_file as a source-read tool.
        full = "line\n" * 4000  # exceeds default result_char_limit (6000 chars)
        toolset = MockReconToolPort(results={"read_file:/src/x.c": full})
        llm = MockLLMPort([
            LLMToolResponse(
                content="",
                tool_calls=[ToolCall(id="t1", name="read_file", arguments={"path": "/src/x.c"})],
                usage=_usage(),
                model="m",
                cost_usd=0.001,
            ),
            LLMToolResponse(content="done", tool_calls=[], usage=_usage(), model="m", cost_usd=0.0),
        ])
        ctx = _tool_context(toolset)
        ctx = ActiveToolContext(
            tool_definitions=ctx.tool_definitions,
            loop_policy=ctx.loop_policy,
            executors=ctx.executors,
            source_read_tools=frozenset({"read_file"}),
        )

        # When
        result = await ToolCallingService(llm).run_with_tools("prompt", {"model": "m"}, ctx)

        # Then: the FULL pre-truncation content is captured (not the 500-char summary)
        assert len(result.captured_reads) == 1
        read = result.captured_reads[0]
        assert read.tool_name == "read_file"
        assert read.arguments["path"] == "/src/x.c"
        assert read.content == full

    @pytest.mark.asyncio
    async def test_no_capture_when_source_read_tools_empty(self):
        # Given: the same read_file call but the default (empty) capture set
        toolset = MockReconToolPort()
        llm = MockLLMPort([
            LLMToolResponse(
                content="",
                tool_calls=[ToolCall(id="t1", name="read_file", arguments={"path": "/src/x.c"})],
                usage=_usage(),
                model="m",
                cost_usd=0.001,
            ),
            LLMToolResponse(content="done", tool_calls=[], usage=_usage(), model="m", cost_usd=0.0),
        ])

        # When
        result = await ToolCallingService(llm).run_with_tools(
            "prompt", {"model": "m"}, _tool_context(toolset)
        )

        # Then: nothing captured (byte-identical to legacy behavior)
        assert result.captured_reads == []


class TestFreezeOnceMasking:
    """Aged transient results are elided once and stay byte-stable; source reads kept."""

    def _transcript(self):
        mk_asst = lambda cid, name: {
            "role": "assistant",
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": name, "arguments": "{}"}}],
        }
        return [
            {"role": "user", "content": "prompt"},
            mk_asst("c1", "read_file"),
            {"role": "tool", "tool_call_id": "c1", "content": "int x; // verbatim source"},
            mk_asst("c2", "search_codebase"),
            {"role": "tool", "tool_call_id": "c2", "content": "hit1\nhit2"},
            mk_asst("c3", "search_codebase"),
            {"role": "tool", "tool_call_id": "c3", "content": "most recent search"},
        ]

    def test_source_read_kept_transient_elided_and_byte_stable(self):
        from core.application.services.toolset.tool_calling_service import (
            _freeze_aged_tool_results,
        )

        # Given: a 3-round transcript, boundary = the last assistant block (c3)
        msgs = self._transcript()
        frozen: set[int] = set()

        # When: freezing once
        _freeze_aged_tool_results(msgs, frozenset({"read_file"}), frozen)

        # Then: read_file (idx2) kept verbatim; aged search (idx4) elided;
        # most-recent search (idx6, after boundary) stays verbatim
        assert msgs[2]["content"] == "int x; // verbatim source"
        assert "elided" in msgs[4]["content"]
        assert msgs[6]["content"] == "most recent search"
        snapshot = [m.get("content") for m in msgs]

        # And: a second freeze call (no new messages) changes nothing — the
        # already-aged bytes are stable, which the rolling cache prefix needs.
        _freeze_aged_tool_results(msgs, frozenset({"read_file"}), frozen)
        assert [m.get("content") for m in msgs] == snapshot

    def test_honest_stub_wording_no_dangling_pointer(self):
        from core.application.services.toolset.tool_calling_service import (
            _freeze_aged_tool_results,
        )

        # Given/When
        msgs = self._transcript()
        _freeze_aged_tool_results(msgs, frozenset({"read_file"}), set())

        # Then: the stub does not promise a recoverable "earlier call"
        assert "see earlier call" not in msgs[4]["content"]
        assert "not retained" in msgs[4]["content"]


class TestReadFileDedup:
    """A repeated read of the same path is sent once; later reads point at it."""

    @pytest.mark.asyncio
    async def test_same_path_second_read_is_pointer_stub(self):
        toolset = MockReconToolPort(results={"read_file:/x.c": "SOURCE-BODY-OF-X"})
        responses = [
            LLMToolResponse(content="",
                tool_calls=[ToolCall(id="a", name="read_file", arguments={"path": "/x.c"})],
                usage=_usage(), model="m", cost_usd=0.0),
            LLMToolResponse(content="",
                tool_calls=[ToolCall(id="b", name="read_file", arguments={"path": "/x.c"})],
                usage=_usage(), model="m", cost_usd=0.0),
            LLMToolResponse(content="done", tool_calls=[], usage=_usage(), model="m", cost_usd=0.0),
        ]
        llm = MockLLMPort(responses)
        ctx = _tool_context(toolset)
        ctx = ActiveToolContext(
            tool_definitions=ctx.tool_definitions, loop_policy=ctx.loop_policy,
            executors=ctx.executors, source_read_tools=frozenset({"read_file"}),
        )

        # When
        result = await ToolCallingService(llm).run_with_tools("p", {"model": "m"}, ctx)

        # Then: full content captured BOTH times (for the shared block)...
        assert [r.content for r in result.captured_reads] == ["SOURCE-BODY-OF-X", "SOURCE-BODY-OF-X"]
        # ...but the second read's MESSAGE is a pointer stub, not the re-sent body.
        tool_msgs = [m for m in llm.calls[-1]["messages"] if m.get("role") == "tool"]
        assert tool_msgs[0]["content"] == "SOURCE-BODY-OF-X"
        assert "not re-sent" in tool_msgs[1]["content"]
        assert "SOURCE-BODY-OF-X" not in tool_msgs[1]["content"]


class TestRollingTailCacheBreakpoint:
    """The last message gets an ephemeral breakpoint on Anthropic, no-op elsewhere."""

    def test_anthropic_tail_gets_cache_control(self):
        from infrastructure.adapters.litellm_adapter import _apply_anthropic_cache_to_tail

        msgs = [{"role": "user", "content": "p"},
                {"role": "tool", "tool_call_id": "c1", "content": "tool result"}]
        out = _apply_anthropic_cache_to_tail(msgs, "claude-sonnet-4-6")

        last = out[-1]["content"]
        assert isinstance(last, list)
        assert last[-1]["cache_control"] == {"type": "ephemeral"}
        assert last[-1]["text"] == "tool result"
        # original not mutated
        assert msgs[-1]["content"] == "tool result"

    def test_non_anthropic_is_noop(self):
        from infrastructure.adapters.litellm_adapter import _apply_anthropic_cache_to_tail

        msgs = [{"role": "user", "content": "p"},
                {"role": "tool", "tool_call_id": "c1", "content": "r"}]
        assert _apply_anthropic_cache_to_tail(msgs, "gpt-5.4-mini") == msgs

    def test_single_message_is_noop(self):
        from infrastructure.adapters.litellm_adapter import _apply_anthropic_cache_to_tail

        msgs = [{"role": "user", "content": "p"}]
        assert _apply_anthropic_cache_to_tail(msgs, "claude-sonnet-4-6") == msgs
