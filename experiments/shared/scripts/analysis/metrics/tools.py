"""Tool-metric computer over a run's event stream (design doc §5, §6).

Aggregates ``ThoughtCaptured(output_type='tool_use')`` rows into the
``ToolMetrics`` dataclass. Tool identity is recovered via
``recover_tool_name`` (handles both A1/A2 prefix-encoded content and the
B1 SDK structured field). Per-family semantic categorisation is driven
by ``TOOL_TAXONOMY``; unknown families raise ``UnknownFamilyError``
rather than silently classifying every tool as ``OTHER``.
"""

from __future__ import annotations

import enum
import re
from collections import Counter
from typing import TYPE_CHECKING, Final

from experiments.shared.scripts.analysis.errors import UnknownFamilyError
from experiments.shared.scripts.analysis.models import ToolMetrics
from experiments.shared.scripts.analysis.text.bash_classifier import (
    classify_bash_command,
)
from experiments.shared.scripts.analysis.text.forbidden_web import (
    # Re-using the private ``_extract_bash_command`` helper is justified
    # here: it is the same JSON-extraction routine the design doc §9
    # mandates for Bash content, and duplicating it would violate DRY.
    SHELL_TOOL_NAMES,
    _extract_bash_command,
    detect_violations,
)
from experiments.shared.scripts.analysis.text.prefixes import recover_tool_name


if TYPE_CHECKING:
    from collections.abc import Sequence

    from experiments.shared.scripts.db.models import EventRow


class ToolCategory(str, enum.Enum):
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_EDIT = "file_edit"
    SEARCH = "search"
    SHELL = "shell"
    TASK_MGMT = "task_mgmt"
    SUBAGENT_SPAWN = "subagent_spawn"
    WEB_FORBIDDEN = "web_forbidden"
    MCP = "mcp"
    OTHER = "other"


_MCP_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^mcp__")


TOOL_TAXONOMY: Final[dict[str, dict[ToolCategory, frozenset[str]]]] = {
    "A": {
        ToolCategory.FILE_READ: frozenset({"Read"}),
        ToolCategory.FILE_WRITE: frozenset({"Write"}),
        ToolCategory.FILE_EDIT: frozenset({"Edit", "MultiEdit"}),
        ToolCategory.SEARCH: frozenset({"Glob", "Grep", "ToolSearch"}),
        ToolCategory.SHELL: SHELL_TOOL_NAMES,
        ToolCategory.TASK_MGMT: frozenset(
            {
                "TaskCreate",
                "TaskUpdate",
                "TaskList",
                "TaskGet",
                "TaskStop",
                "TaskOutput",
                "TodoWrite",
            }
        ),
        ToolCategory.SUBAGENT_SPAWN: frozenset({"Task", "Agent"}),
        ToolCategory.WEB_FORBIDDEN: frozenset({"WebFetch", "WebSearch"}),
        # MCP / OTHER are handled outside the table by classify_tool().
    },
}


def classify_tool(family: str, tool_name: str) -> ToolCategory:
    """Return the category for ``tool_name`` within ``family``'s taxonomy.

    Raises ``UnknownFamilyError`` when ``family`` is not declared in
    ``TOOL_TAXONOMY``. MCP membership is decided by a name-prefix
    predicate (``mcp__...`` or the literal ``security_tools``) before the
    per-family table is consulted, since MCP servers are family-agnostic.
    """
    table = TOOL_TAXONOMY.get(family)
    if table is None:
        raise UnknownFamilyError(family=family)
    if _MCP_PREFIX_RE.match(tool_name) or tool_name == "security_tools":
        return ToolCategory.MCP
    for category, names in table.items():
        if tool_name in names:
            return category
    return ToolCategory.OTHER


def compute_tools(
    events: Sequence[EventRow],
    *,
    family: str = "A",
) -> ToolMetrics:
    """Aggregate tool-use metrics over the events for the given family.

    Fail fast on unknown families at the metric layer — better than
    silently treating every tool as ``OTHER``.
    """
    if family not in TOOL_TAXONOMY:
        raise UnknownFamilyError(family=family)

    by_tool_name: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    bash_subtypes: Counter[str] = Counter()
    total = 0
    for e in events:
        if e.event_type != "ThoughtCaptured":
            continue
        p = e.payload
        if p.get("output_type") != "tool_use":
            continue
        total += 1
        content = p.get("content") or ""
        tool_name = recover_tool_name(content, p.get("tool_name"))
        by_tool_name[tool_name] += 1
        category = classify_tool(family, tool_name)
        by_category[category.value] += 1
        if category is ToolCategory.SHELL:
            cmd = _extract_bash_command(content) or ""
            bash_subtypes[classify_bash_command(cmd).value] += 1

    subagent_spawn_count = by_category.get(ToolCategory.SUBAGENT_SPAWN.value, 0)

    violations = detect_violations(events)
    breakdown: Counter[str] = Counter(v.via for v in violations)

    return ToolMetrics(
        total_tool_calls=total,
        by_tool_name=dict(by_tool_name),
        by_category=dict(by_category),
        bash_subtypes=dict(bash_subtypes),
        subagent_spawn_count=subagent_spawn_count,
        forbidden_web_attempts=len(violations),
        forbidden_web_breakdown=dict(breakdown),
        forbidden_web_violations=tuple(violations),
    )
