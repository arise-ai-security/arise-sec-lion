"""Tree summary generator for BOSS agent hierarchies.

Generates comprehensive summaries of all work done by agents in a subtree,
including worker reports and justifications.
"""

from collections import deque
from dataclasses import dataclass, field
from uuid import UUID

from core.domain.events import ChildSpawned, SubordinatesSpawned
from core.domain.model import AgentRole, AgentSession, AgentStatus
from core.domain.subtask import WorkerReport
from core.ports.event_store_port import EventStorePort


@dataclass
class AgentSummary:
    """Summary of a single agent's work."""

    agent_id: str
    role: str
    status: str
    task_description: str
    result: str | None
    error_message: str | None
    worker_report: WorkerReport | None
    depth: int
    children: list["AgentSummary"] = field(default_factory=list)


@dataclass
class TreeSummary:
    """Complete summary of work done by a BOSS agent's subtree."""

    boss_id: str
    task: str
    status: str
    total_agents: int
    completed_agents: int
    failed_agents: int
    worker_count: int
    manager_count: int
    max_depth: int
    root: AgentSummary
    worker_reports: list[dict]  # Flattened list of worker reports with context
    aggregated_deliverables: str


class TreeSummaryGenerator:
    """Generates comprehensive summaries of agent subtrees.

    This collects all worker reports and generates a comprehensive summary
    of all work done by agents in the hierarchy.
    """

    def __init__(self, event_store: EventStorePort) -> None:
        self._event_store = event_store

    async def generate(self, root_agent_id: UUID) -> TreeSummary:
        """Generate a complete tree summary from the BOSS agent down.

        Args:
            root_agent_id: UUID of the BOSS agent at the root.

        Returns:
            TreeSummary with all agent summaries and aggregated information.
        """
        # Build the tree structure
        root_summary, stats = await self._build_tree(root_agent_id)

        # Collect all worker reports
        worker_reports = self._collect_worker_reports(root_summary)

        # Generate aggregated deliverables
        aggregated_deliverables = self._aggregate_deliverables(worker_reports)

        # Load root agent for task info
        root_events = await self._event_store.get_events(root_agent_id)
        root_agent = AgentSession.load_from_history(root_events)

        return TreeSummary(
            boss_id=str(root_agent_id),
            task=root_agent.task_description or "",
            status=root_agent.status.value,
            total_agents=stats["total"],
            completed_agents=stats["completed"],
            failed_agents=stats["failed"],
            worker_count=stats["workers"],
            manager_count=stats["managers"],
            max_depth=stats["max_depth"],
            root=root_summary,
            worker_reports=worker_reports,
            aggregated_deliverables=aggregated_deliverables,
        )

    async def _build_tree(
        self, root_id: UUID
    ) -> tuple[AgentSummary, dict]:
        """Build tree structure and collect stats via BFS."""
        stats = {
            "total": 0,
            "completed": 0,
            "failed": 0,
            "workers": 0,
            "managers": 0,
            "max_depth": 0,
        }

        # Map for parent-child relationships
        summaries: dict[UUID, AgentSummary] = {}
        parent_map: dict[UUID, UUID] = {}

        # BFS traversal
        visited: set[UUID] = set()
        queue: deque[tuple[UUID, int]] = deque([(root_id, 0)])

        while queue:
            agent_id, depth = queue.popleft()
            if agent_id in visited:
                continue
            visited.add(agent_id)

            events = await self._event_store.get_events(agent_id)
            if not events:
                continue

            agent = AgentSession.load_from_history(events)
            stats["total"] += 1
            stats["max_depth"] = max(stats["max_depth"], depth)

            if agent.status == AgentStatus.COMPLETED:
                stats["completed"] += 1
            elif agent.status == AgentStatus.FAILED:
                stats["failed"] += 1

            if agent.role == AgentRole.WORKER:
                stats["workers"] += 1
            elif agent.role in (AgentRole.MANAGER, AgentRole.BOSS):
                stats["managers"] += 1

            summary = AgentSummary(
                agent_id=str(agent_id)[:8],
                role=agent.role.value,
                status=agent.status.value,
                task_description=agent.task_description or "",
                result=agent.result,
                error_message=agent.error_message,
                worker_report=agent.worker_report,
                depth=depth,
            )
            summaries[agent_id] = summary

            # Find children
            for event in events:
                if isinstance(event, ChildSpawned):
                    if event.child_id not in visited:
                        queue.append((event.child_id, depth + 1))
                        parent_map[event.child_id] = agent_id
                elif isinstance(event, SubordinatesSpawned):
                    for config in event.subordinate_configs:
                        child_id = config.get("child_id")
                        if child_id:
                            if isinstance(child_id, str):
                                child_id = UUID(child_id)
                            if child_id not in visited:
                                queue.append((child_id, depth + 1))
                                parent_map[child_id] = agent_id

        # Build tree structure
        for child_id, parent_id in parent_map.items():
            if child_id in summaries and parent_id in summaries:
                summaries[parent_id].children.append(summaries[child_id])

        return summaries.get(root_id, AgentSummary(
            agent_id=str(root_id)[:8],
            role="unknown",
            status="unknown",
            task_description="",
            result=None,
            error_message=None,
            worker_report=None,
            depth=0,
        )), stats

    def _collect_worker_reports(
        self, root: AgentSummary
    ) -> list[dict]:
        """Collect all worker reports from the tree in DFS order."""
        reports = []

        def traverse(summary: AgentSummary):
            if summary.worker_report is not None:
                reports.append({
                    "agent_id": summary.agent_id,
                    "task": summary.task_description[:100] if summary.task_description else "",
                    "original_task": summary.worker_report.original_task[:200] if summary.worker_report.original_task else "",
                    "approach": summary.worker_report.approach,
                    "reasoning": summary.worker_report.reasoning,
                    "deliverables": summary.worker_report.deliverables,
                    "challenges": summary.worker_report.challenges,
                    "result": summary.result[:200] if summary.result else "",
                    "depth": summary.depth,
                })
            for child in summary.children:
                traverse(child)

        traverse(root)
        return reports

    def _aggregate_deliverables(self, worker_reports: list[dict]) -> str:
        """Generate aggregated summary of all deliverables."""
        if not worker_reports:
            return "No worker reports available."

        lines = [
            "## Work Completed by All Workers",
            "",
            f"Total workers: {len(worker_reports)}",
            "",
        ]

        for i, report in enumerate(worker_reports, 1):
            lines.append(f"### Worker {i} [{report['agent_id']}]")
            lines.append(f"**Task:** {report['task']}")
            lines.append(f"**Approach:** {report['approach']}")
            lines.append(f"**Reasoning:** {report['reasoning']}")
            lines.append(f"**Deliverables:** {report['deliverables']}")
            if report['challenges'] and report['challenges'] != "No significant challenges encountered":
                lines.append(f"**Challenges:** {report['challenges']}")
            lines.append("")

        return "\n".join(lines)


def format_tree_summary_text(summary: TreeSummary) -> str:
    """Format a TreeSummary as human-readable text for CLI display."""
    lines = [
        "=" * 80,
        "BOSS TREE SUMMARY",
        "=" * 80,
        "",
        f"Task: {summary.task}",
        f"Status: {summary.status.upper()}",
        "",
        "--- Statistics ---",
        f"Total Agents: {summary.total_agents}",
        f"  - Workers: {summary.worker_count}",
        f"  - Managers: {summary.manager_count}",
        f"  - Completed: {summary.completed_agents}",
        f"  - Failed: {summary.failed_agents}",
        f"Max Depth: {summary.max_depth}",
        "",
        "--- Work Summary ---",
        "",
    ]

    # Add worker reports
    if summary.worker_reports:
        for i, report in enumerate(summary.worker_reports, 1):
            lines.append(f"Worker {i} [{report['agent_id']}]:")
            lines.append(f"  Task: {report['task'][:60]}...")
            lines.append(f"  Approach: {report['approach'][:80]}...")
            lines.append(f"  Deliverables: {report['deliverables'][:80]}...")
            if report['challenges'] and report['challenges'] != "No significant challenges encountered":
                lines.append(f"  Challenges: {report['challenges'][:60]}...")
            lines.append("")
    else:
        lines.append("No worker reports available.")
        lines.append("")

    lines.append("=" * 80)

    return "\n".join(lines)


def format_tree_summary_json(summary: TreeSummary) -> dict:
    """Format a TreeSummary as JSON-serializable dict."""
    def agent_to_dict(agent: AgentSummary) -> dict:
        return {
            "agent_id": agent.agent_id,
            "role": agent.role,
            "status": agent.status,
            "task_description": agent.task_description,
            "result": agent.result,
            "error_message": agent.error_message,
            "worker_report": (
                {
                    "original_task": agent.worker_report.original_task,
                    "approach": agent.worker_report.approach,
                    "reasoning": agent.worker_report.reasoning,
                    "deliverables": agent.worker_report.deliverables,
                    "challenges": agent.worker_report.challenges,
                }
                if agent.worker_report
                else None
            ),
            "depth": agent.depth,
            "children": [agent_to_dict(c) for c in agent.children],
        }

    return {
        "boss_id": summary.boss_id,
        "task": summary.task,
        "status": summary.status,
        "statistics": {
            "total_agents": summary.total_agents,
            "completed_agents": summary.completed_agents,
            "failed_agents": summary.failed_agents,
            "worker_count": summary.worker_count,
            "manager_count": summary.manager_count,
            "max_depth": summary.max_depth,
        },
        "worker_reports": summary.worker_reports,
        "aggregated_deliverables": summary.aggregated_deliverables,
        "hierarchy": agent_to_dict(summary.root),
    }
