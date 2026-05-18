"""Unit tests for ``metrics.tools`` (design doc §5, §6, §13)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from experiments.shared.scripts.analysis.errors import UnknownFamilyError
from experiments.shared.scripts.analysis.metrics.tools import (
    ToolCategory,
    classify_tool,
    compute_tools,
)
from experiments.shared.scripts.analysis.tests.factories import (
    event_row,
    tool_use_event,
)


# ---------------------------------------------------------------------------
# classify_tool
# ---------------------------------------------------------------------------


class TestClassifyToolATaxonomy:
    """Each ``ToolCategory`` mapped from the A taxonomy resolves correctly."""

    @pytest.mark.parametrize(
        ("tool_name", "expected"),
        [
            # FILE_READ
            ("Read", ToolCategory.FILE_READ),
            # FILE_WRITE
            ("Write", ToolCategory.FILE_WRITE),
            # FILE_EDIT — both members
            ("Edit", ToolCategory.FILE_EDIT),
            ("MultiEdit", ToolCategory.FILE_EDIT),
            # SEARCH — representative members
            ("Glob", ToolCategory.SEARCH),
            ("Grep", ToolCategory.SEARCH),
            ("ToolSearch", ToolCategory.SEARCH),
            # SHELL — both members. Monitor (Claude Code v2.1.105+) runs Bash
            # via ``bash -c`` and shares the SHELL classification.
            ("Bash", ToolCategory.SHELL),
            ("Monitor", ToolCategory.SHELL),
            # TASK_MGMT — representative members
            ("TaskCreate", ToolCategory.TASK_MGMT),
            ("TaskUpdate", ToolCategory.TASK_MGMT),
            ("TodoWrite", ToolCategory.TASK_MGMT),
            # SUBAGENT_SPAWN
            ("Task", ToolCategory.SUBAGENT_SPAWN),
            ("Agent", ToolCategory.SUBAGENT_SPAWN),
            # WEB_FORBIDDEN — both members
            ("WebFetch", ToolCategory.WEB_FORBIDDEN),
            ("WebSearch", ToolCategory.WEB_FORBIDDEN),
        ],
    )
    def test_taxonomy_entries_classified_correctly(self, tool_name, expected):
        # Given: a tool name from the A taxonomy
        # When: classified under family "A"
        actual = classify_tool("A", tool_name)
        # Then: the expected category is returned
        assert actual == expected


class TestClassifyToolMCPPredicate:
    """MCP membership is decided by the name-prefix predicate, not the table."""

    def test_mcp_prefixed_name_is_mcp_category(self):
        # Given: an MCP-prefixed security tool name
        # When: classified under family "A"
        actual = classify_tool("A", "mcp__security_tools__shell_in_container")
        # Then: MCP wins regardless of family-table contents
        assert actual == ToolCategory.MCP

    def test_security_tools_literal_is_mcp_category(self):
        # Given: the bare ``security_tools`` literal
        # When: classified under family "A"
        actual = classify_tool("A", "security_tools")
        # Then: MCP is returned (special-case literal)
        assert actual == ToolCategory.MCP


class TestClassifyToolFallthrough:
    def test_unknown_tool_returns_other(self):
        # Given: a tool name absent from the A taxonomy
        # When: classified under family "A"
        actual = classify_tool("A", "RandomUnknownTool")
        # Then: OTHER is the documented fallthrough
        assert actual == ToolCategory.OTHER

    def test_skill_tool_is_other(self):
        # Given: the Claude Code ``Skill`` invocation tool, observed in the
        # event store but intentionally not in any semantic taxonomy bucket
        # (it executes an in-conversation skill, not file/shell/search/etc.)
        # When: classified under family "A"
        actual = classify_tool("A", "Skill")
        # Then: OTHER is the deliberate classification
        assert actual == ToolCategory.OTHER


class TestClassifyToolUnknownFamily:
    def test_family_b_raises_unknown_family_error(self):
        # Given: family "B" — taxonomy unpopulated per design doc §6
        # When/Then: classify_tool raises UnknownFamilyError
        with pytest.raises(UnknownFamilyError) as exc_info:
            classify_tool("B", "Read")
        # And: the error carries the offending family attribute
        assert exc_info.value.family == "B"

    def test_family_c_raises_unknown_family_error(self):
        # Given: family "C" — taxonomy unpopulated per design doc §6
        # When/Then: classify_tool raises UnknownFamilyError
        with pytest.raises(UnknownFamilyError) as exc_info:
            classify_tool("C", "Read")
        assert exc_info.value.family == "C"


# ---------------------------------------------------------------------------
# compute_tools — happy paths
# ---------------------------------------------------------------------------


class TestComputeToolsShellPath:
    def test_single_bash_event_classified_as_shell_other(self):
        # Given: a single innocuous Bash event on the A1/A2 prefix path
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=1,
                tool_name="Bash",
                tool_input={"command": "ls -la"},
            )
        ]

        # When
        m = compute_tools(events, family="A")

        # Then: totals reflect the one tool call
        assert m.total_tool_calls == 1
        # And: by_category counts under "shell"
        assert m.by_category == {"shell": 1}
        # And: bash_subtypes resolves to OTHER_SHELL ("other_shell")
        assert m.bash_subtypes == {"other_shell": 1}
        # And: no Task spawn occurred
        assert m.subagent_spawn_count == 0
        # And: no forbidden web attempts detected
        assert m.forbidden_web_attempts == 0
        assert m.forbidden_web_breakdown == {}
        assert m.forbidden_web_violations == ()

    def test_bash_curl_event_records_web_subtype_and_violation(self):
        # Given: a Bash ``curl https://x`` event — a forbidden-web attempt
        # via shell
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=7,
                tool_name="Bash",
                tool_input={"command": "curl https://example.com"},
            )
        ]

        # When
        m = compute_tools(events, family="A")

        # Then: shell bucket sees the call; subtype is web_via_shell
        assert m.bash_subtypes == {"web_via_shell": 1}
        # And: exactly one forbidden-web violation tagged as bash_command
        assert m.forbidden_web_attempts == 1
        assert m.forbidden_web_breakdown == {"bash_command": 1}
        assert len(m.forbidden_web_violations) == 1
        v = m.forbidden_web_violations[0]
        # And: the URL is captured in the evidence snippet
        assert "https://example.com" in v.evidence_snippet


class TestComputeToolsMixedCategories:
    def test_mixed_event_set_populates_each_category(self):
        # Given: one event per A-taxonomy category representative
        agg = uuid4()
        events = [
            tool_use_event(agg, 1, "Bash", {"command": "ls"}),
            tool_use_event(agg, 2, "Read", {"file_path": "/x"}),
            tool_use_event(agg, 3, "Write", {"file_path": "/y"}),
            tool_use_event(agg, 4, "Edit", {"file_path": "/z"}),
            tool_use_event(agg, 5, "MultiEdit", {"file_path": "/w"}),
            tool_use_event(agg, 6, "Grep", {"pattern": "foo"}),
            tool_use_event(agg, 7, "Glob", {"pattern": "*.py"}),
            tool_use_event(agg, 8, "ToolSearch", {"query": "anything"}),
        ]

        # When
        m = compute_tools(events, family="A")

        # Then: every entry counted
        assert m.total_tool_calls == 8
        # And: by_category reflects the taxonomy buckets
        assert m.by_category == {
            "shell": 1,
            "file_read": 1,
            "file_write": 1,
            "file_edit": 2,  # Edit + MultiEdit collapse into FILE_EDIT
            "search": 3,  # Grep + Glob + ToolSearch
        }
        # And: by_tool_name preserves the recovered raw names
        assert m.by_tool_name == {
            "Bash": 1,
            "Read": 1,
            "Write": 1,
            "Edit": 1,
            "MultiEdit": 1,
            "Grep": 1,
            "Glob": 1,
            "ToolSearch": 1,
        }


class TestComputeToolsSubagentSpawn:
    def test_task_event_counts_as_subagent_spawn(self):
        # Given: a single ``Task`` invocation (sub-agent spawn)
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=1,
                tool_name="Task",
                tool_input={"description": "delegate"},
                set_structured_field=True,
            )
        ]

        # When
        m = compute_tools(events, family="A")

        # Then: subagent_spawn_count is exact and category bucketed
        assert m.subagent_spawn_count == 1
        assert m.by_category == {"subagent_spawn": 1}
        assert m.by_tool_name == {"Task": 1}

    def test_agent_event_counts_as_subagent_spawn(self):
        # Given: Claude Code stream output naming a delegated sub-agent as Agent
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=1,
                tool_name="Agent",
                tool_input={"prompt": "delegate"},
                set_structured_field=True,
            )
        ]

        # When
        m = compute_tools(events, family="A")

        # Then: the stream-json alias is counted as subagent delegation
        assert m.subagent_spawn_count == 1
        assert m.by_category == {"subagent_spawn": 1}
        assert m.by_tool_name == {"Agent": 1}


class TestComputeToolsTaskMgmt:
    def test_taskcreate_taskupdate_tasklist_aggregate_into_task_mgmt(self):
        # Given: three task-management tool calls
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=1,
                tool_name="TaskCreate",
                tool_input={"description": "x"},
                set_structured_field=True,
            ),
            tool_use_event(
                agg,
                seq=2,
                tool_name="TaskUpdate",
                tool_input={"id": "y"},
                set_structured_field=True,
            ),
            tool_use_event(
                agg,
                seq=3,
                tool_name="TaskList",
                tool_input={},
                set_structured_field=True,
            ),
        ]

        # When
        m = compute_tools(events, family="A")

        # Then: all three collapse into the task_mgmt bucket
        assert m.by_category == {"task_mgmt": 3}


class TestComputeToolsWebForbidden:
    def test_webfetch_event_counted_as_web_forbidden_and_violation(self):
        # Given: a direct WebFetch invocation
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=42,
                tool_name="WebFetch",
                tool_input={"url": "https://example.com"},
                set_structured_field=True,
            )
        ]

        # When
        m = compute_tools(events, family="A")

        # Then: web_forbidden category, forbidden_web_attempts increments
        assert m.by_category == {"web_forbidden": 1}
        assert m.forbidden_web_attempts == 1
        assert m.forbidden_web_breakdown == {"direct_tool": 1}
        assert len(m.forbidden_web_violations) == 1
        assert m.forbidden_web_violations[0].via == "direct_tool"
        assert m.forbidden_web_violations[0].tool_or_cmd == "WebFetch"


class TestComputeToolsMCP:
    def test_mcp_security_tool_event_is_mcp_category(self):
        # Given: an MCP shell-in-container tool call
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=1,
                tool_name="mcp__security_tools__shell_in_container",
                tool_input={"command": "ls"},
                set_structured_field=True,
            )
        ]

        # When
        m = compute_tools(events, family="A")

        # Then: routed into the MCP bucket; not a Bash classification
        assert m.by_category == {"mcp": 1}
        # And: no bash subtype recorded (MCP, not SHELL)
        assert m.bash_subtypes == {}


# ---------------------------------------------------------------------------
# compute_tools — unknown family
# ---------------------------------------------------------------------------


class TestComputeToolsUnknownFamily:
    def test_family_b_raises_at_metric_layer(self):
        # Given: arbitrary events and an unsupported family
        agg = uuid4()
        events = [
            tool_use_event(agg, seq=1, tool_name="Read", tool_input={"file_path": "/x"}),
        ]

        # When/Then: compute_tools fails fast on unknown family
        with pytest.raises(UnknownFamilyError) as exc_info:
            compute_tools(events, family="B")
        assert exc_info.value.family == "B"


# ---------------------------------------------------------------------------
# compute_tools — edge cases
# ---------------------------------------------------------------------------


class TestComputeToolsEmptyAndMixedPaths:
    def test_empty_events_yields_zero_totals_and_empty_dicts(self):
        # Given: no events at all
        # When
        m = compute_tools([], family="A")
        # Then: every field is the empty default
        assert m.total_tool_calls == 0
        assert m.by_tool_name == {}
        assert m.by_category == {}
        assert m.bash_subtypes == {}
        assert m.subagent_spawn_count == 0
        assert m.forbidden_web_attempts == 0
        assert m.forbidden_web_breakdown == {}
        assert m.forbidden_web_violations == ()

    def test_a_prefix_path_and_b_structured_path_coexist(self):
        # Given: a mixture of A1/A2-shape rows (no structured field —
        # tool name recovered from content prefix) and B1-shape rows
        # (structured field populated)
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=1,
                tool_name="Read",
                tool_input={"file_path": "/a"},
                set_structured_field=False,
            ),
            tool_use_event(
                agg,
                seq=2,
                tool_name="Read",
                tool_input={"file_path": "/b"},
                set_structured_field=True,
            ),
            tool_use_event(
                agg,
                seq=3,
                tool_name="MultiEdit",
                tool_input={"file_path": "/c"},
                set_structured_field=True,
            ),
        ]

        # When
        m = compute_tools(events, family="A")

        # Then: both paths route through classify_tool identically
        assert m.total_tool_calls == 3
        assert m.by_tool_name == {"Read": 2, "MultiEdit": 1}
        assert m.by_category == {"file_read": 2, "file_edit": 1}

    def test_non_tool_use_thoughtcaptured_is_ignored(self):
        # Given: a ThoughtCaptured row with output_type != tool_use
        # alongside one real tool_use row
        agg = uuid4()
        events = [
            event_row(
                agg,
                seq=1,
                event_type="ThoughtCaptured",
                payload={
                    "output_type": "output",
                    "content": "Tool: Bash\nInput: {}",
                    "tool_name": None,
                    "stream": "claude_code",
                },
            ),
            tool_use_event(
                agg,
                seq=2,
                tool_name="Read",
                tool_input={"file_path": "/x"},
            ),
        ]

        # When
        m = compute_tools(events, family="A")

        # Then: only the actual tool_use row is counted
        assert m.total_tool_calls == 1
        assert m.by_tool_name == {"Read": 1}
