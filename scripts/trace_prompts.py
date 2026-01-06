#!/usr/bin/env python3
"""Trace prompt flow through agent hierarchy for a given run.

Shows what prompts each node received and passed to children/siblings.

Usage:
    python trace_prompts.py <run_id> [options]
    python trace_prompts.py 455a6a46-4598-4375-af53-d03898c4f762
    python trace_prompts.py 455a6a46-4598-4375-af53-d03898c4f762 --role worker
    python trace_prompts.py 455a6a46-4598-4375-af53-d03898c4f762 --depth 2
    python trace_prompts.py 455a6a46-4598-4375-af53-d03898c4f762 --agent <agent_id>
    python trace_prompts.py 455a6a46-4598-4375-af53-d03898c4f762 --format json
"""

import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

EVENTS_CSV = Path(__file__).parent.parent / "output" / "events.csv"


@dataclass
class AgentNode:
    agent_id: str
    role: str = "pending"
    task: str = ""
    parent_id: Optional[str] = None
    depth: int = 0
    prompts: list = field(default_factory=list)
    children: list = field(default_factory=list)


def load_events_for_run(csv_path: Path, run_id: str) -> tuple[dict, list]:
    """Load events and build agent tree starting from run_id."""
    # First pass: identify all agents in the tree
    parent_children = defaultdict(list)
    agent_data = {}
    all_events = []

    with open(csv_path, "r") as f:
        for row in csv.DictReader(f):
            all_events.append(row)

    # Build parent-child relationships and collect agent metadata
    for row in all_events:
        agent_id = row["aggregate_id"]
        event_type = row["event_type"]

        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue

        if event_type == "AgentCreated":
            parent_id = payload.get("parent_id")
            role = payload.get("role", "pending")
            spawn_payload = payload.get("spawn_payload") or {}
            depth = spawn_payload.get("depth", 0)

            agent_data[agent_id] = {
                "parent_id": parent_id,
                "role": role,
                "depth": depth,
                "task": "",
                "prompts": [],
            }
            if parent_id:
                parent_children[parent_id].append(agent_id)

        elif event_type == "TaskAssigned":
            if agent_id not in agent_data:
                agent_data[agent_id] = {"parent_id": None, "role": "boss", "depth": 0, "task": "", "prompts": []}
            agent_data[agent_id]["task"] = payload.get("task_description", "")

        elif event_type == "ComplexityEvaluated":
            if agent_id in agent_data:
                decision = payload.get("decision", "")
                if decision == "complex":
                    agent_data[agent_id]["role"] = "manager"
                elif decision == "simple":
                    agent_data[agent_id]["role"] = "worker"

        elif event_type == "PromptSent":
            if agent_id not in agent_data:
                agent_data[agent_id] = {"parent_id": None, "role": "unknown", "depth": 0, "task": "", "prompts": []}
            agent_data[agent_id]["prompts"].append({
                "prompt": payload.get("prompt", ""),
                "occurred_at": row["occurred_at"],
            })

    # Find all agents in the run's tree (BFS from run_id)
    tree_agents = set()
    queue = [run_id]
    while queue:
        current = queue.pop(0)
        if current in tree_agents:
            continue
        tree_agents.add(current)
        queue.extend(parent_children.get(current, []))

    # Filter to only agents in this run's tree
    filtered_data = {k: v for k, v in agent_data.items() if k in tree_agents}

    return filtered_data, parent_children


def build_tree(agent_data: dict, parent_children: dict, run_id: str) -> AgentNode:
    """Build hierarchical tree structure."""
    def build_node(agent_id: str) -> AgentNode:
        data = agent_data.get(agent_id, {})
        node = AgentNode(
            agent_id=agent_id,
            role=data.get("role", "unknown"),
            task=data.get("task", ""),
            parent_id=data.get("parent_id"),
            depth=data.get("depth", 0),
            prompts=data.get("prompts", []),
        )
        for child_id in parent_children.get(agent_id, []):
            if child_id in agent_data:
                node.children.append(build_node(child_id))
        # Sort children by first prompt timestamp
        node.children.sort(key=lambda n: n.prompts[0]["occurred_at"] if n.prompts else "")
        return node

    return build_node(run_id)


def extract_prompt_sections(prompt: str) -> dict:
    """Extract key sections from a prompt."""
    sections = {}

    # Parent context
    parent_match = re.search(r"<parent-context>(.*?)</parent-context>", prompt, re.DOTALL)
    if parent_match:
        sections["parent_context"] = parent_match.group(1).strip()

    # Sibling tasks
    sibling_match = re.search(r"<sibling-tasks>(.*?)</sibling-tasks>", prompt, re.DOTALL)
    if sibling_match:
        sections["sibling_context"] = sibling_match.group(1).strip()

    # Task section
    task_match = re.search(r"<TASK>(.*?)</TASK>", prompt, re.DOTALL)
    if task_match:
        sections["task_section"] = task_match.group(1).strip()

    # Role section
    role_match = re.search(r"<ROLE>(.*?)</ROLE>", prompt, re.DOTALL)
    if role_match:
        sections["role"] = role_match.group(1).strip()

    # Global context
    global_match = re.search(r"<global-context>(.*?)</global-context>", prompt, re.DOTALL)
    if global_match:
        sections["global_context"] = global_match.group(1).strip()

    # Instance info extraction
    instance_match = re.search(r"(exiv2|matio|mruby|openjpeg|libarchive|md4c|yara|upx)\.cve-[\d-]+", prompt)
    if instance_match:
        sections["instance"] = instance_match.group(0)

    return sections


def truncate(text: str, max_len: int = 200) -> str:
    """Truncate text with ellipsis."""
    text = text.replace("\n", " ").strip()
    return text[:max_len] + "..." if len(text) > max_len else text


def print_tree(node: AgentNode, indent: int = 0, filter_role: str = None, max_depth: int = None,
               filter_agent: str = None, show_full: bool = False):
    """Print the agent tree with prompt information."""
    prefix = "  " * indent
    role_icons = {"boss": "👔", "manager": "📋", "worker": "⚙️", "pending": "⏳", "unknown": "❓"}

    # Apply filters
    if filter_role and node.role != filter_role:
        for child in node.children:
            print_tree(child, indent, filter_role, max_depth, filter_agent, show_full)
        return

    if max_depth is not None and node.depth > max_depth:
        return

    if filter_agent and node.agent_id != filter_agent:
        for child in node.children:
            print_tree(child, indent, filter_role, max_depth, filter_agent, show_full)
        return

    # Print node header
    icon = role_icons.get(node.role, "❓")
    print(f"\n{prefix}{'='*60}")
    print(f"{prefix}{icon} [{node.role.upper()}] depth={node.depth}")
    print(f"{prefix}   Agent: {node.agent_id}")
    print(f"{prefix}   Task: {truncate(node.task, 100)}")

    # Print prompts
    for i, p in enumerate(node.prompts):
        sections = extract_prompt_sections(p["prompt"])

        print(f"{prefix}   ┌─ Prompt #{i+1} ({p['occurred_at'][:19]})")

        if sections.get("instance"):
            print(f"{prefix}   │  Instance: {sections['instance']}")

        if sections.get("parent_context"):
            print(f"{prefix}   │  📥 FROM PARENT: {truncate(sections['parent_context'], 150)}")

        if sections.get("sibling_context"):
            sibling_preview = truncate(sections["sibling_context"], 200)
            print(f"{prefix}   │  👥 SIBLING DATA: {sibling_preview}")

        if sections.get("role"):
            print(f"{prefix}   │  🎭 ROLE: {truncate(sections['role'], 100)}")

        if sections.get("task_section"):
            print(f"{prefix}   │  📝 TASK: {truncate(sections['task_section'], 150)}")

        if show_full:
            print(f"{prefix}   │  ─── FULL PROMPT ───")
            for line in p["prompt"].split("\n")[:50]:
                print(f"{prefix}   │  {line}")
            if len(p["prompt"].split("\n")) > 50:
                print(f"{prefix}   │  ... (truncated)")

        print(f"{prefix}   └─")

    # Print children info
    if node.children:
        child_roles = defaultdict(int)
        for c in node.children:
            child_roles[c.role] += 1
        child_summary = ", ".join(f"{v} {k}(s)" for k, v in child_roles.items())
        print(f"{prefix}   📤 SPAWNED: {len(node.children)} children ({child_summary})")

    # Recurse to children
    for child in node.children:
        print_tree(child, indent + 1, filter_role, max_depth, filter_agent, show_full)


def print_json(node: AgentNode, filter_role: str = None, max_depth: int = None):
    """Output as JSON for programmatic use."""
    def node_to_dict(n: AgentNode) -> dict:
        if filter_role and n.role != filter_role:
            return None
        if max_depth is not None and n.depth > max_depth:
            return None

        return {
            "agent_id": n.agent_id,
            "role": n.role,
            "depth": n.depth,
            "task": n.task,
            "prompts": [
                {
                    "occurred_at": p["occurred_at"],
                    "sections": extract_prompt_sections(p["prompt"]),
                    "raw_length": len(p["prompt"]),
                }
                for p in n.prompts
            ],
            "children": [c for c in (node_to_dict(child) for child in n.children) if c],
        }

    print(json.dumps(node_to_dict(node), indent=2))


def print_sibling_flow(node: AgentNode, indent: int = 0):
    """Print only sibling-to-sibling data flow (worker chains)."""
    prefix = "  " * indent

    # Find worker groups (siblings)
    worker_groups = []

    def find_worker_groups(n: AgentNode):
        workers = [c for c in n.children if c.role == "worker"]
        if len(workers) > 1:
            worker_groups.append((n, workers))
        for child in n.children:
            find_worker_groups(child)

    find_worker_groups(node)

    print("\n" + "="*70)
    print("SIBLING WORKER DATA FLOW")
    print("="*70)

    for parent, workers in worker_groups:
        print(f"\n📋 Parent Manager: {parent.agent_id[:8]}...")
        print(f"   Task: {truncate(parent.task, 80)}")
        print(f"   Workers: {len(workers)}")
        print()

        for i, worker in enumerate(workers):
            print(f"   ⚙️  Worker #{i+1}: {worker.agent_id[:8]}...")
            print(f"      Task: {truncate(worker.task, 60)}")

            for p in worker.prompts:
                sections = extract_prompt_sections(p["prompt"])
                if sections.get("sibling_context"):
                    # Parse sibling context to show what data was received
                    sibling_text = sections["sibling_context"]
                    completed_match = re.search(r"(\d+) completed", sibling_text)
                    in_progress_match = re.search(r"(\d+) in progress", sibling_text)

                    completed = completed_match.group(1) if completed_match else "?"
                    in_progress = in_progress_match.group(1) if in_progress_match else "?"

                    print(f"      📥 Received: {completed} completed results, {in_progress} in progress")

                    # Extract any result snippets
                    results = re.findall(r'<result>(.*?)</result>', sibling_text, re.DOTALL)
                    for j, result in enumerate(results[:2]):  # Show first 2 results
                        print(f"         └─ Result {j+1}: {truncate(result, 80)}")
            print()


def main():
    args = sys.argv[1:]

    if not args or "-h" in args or "--help" in args:
        print(__doc__)
        sys.exit(0)

    run_id = args[0]

    # Parse options
    filter_role = None
    max_depth = None
    filter_agent = None
    output_format = "tree"
    show_full = False
    show_siblings = False

    i = 1
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            filter_role = args[i + 1]
            i += 2
        elif args[i] == "--depth" and i + 1 < len(args):
            max_depth = int(args[i + 1])
            i += 2
        elif args[i] == "--agent" and i + 1 < len(args):
            filter_agent = args[i + 1]
            i += 2
        elif args[i] == "--format" and i + 1 < len(args):
            output_format = args[i + 1]
            i += 2
        elif args[i] == "--full":
            show_full = True
            i += 1
        elif args[i] == "--siblings":
            show_siblings = True
            i += 1
        else:
            i += 1

    print(f"Loading events for run {run_id}...", file=sys.stderr)
    agent_data, parent_children = load_events_for_run(EVENTS_CSV, run_id)

    if not agent_data:
        print(f"No agents found for run_id: {run_id}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(agent_data)} agents in tree", file=sys.stderr)

    tree = build_tree(agent_data, parent_children, run_id)

    if show_siblings:
        print_sibling_flow(tree)
    elif output_format == "json":
        print_json(tree, filter_role, max_depth)
    else:
        print_tree(tree, filter_role=filter_role, max_depth=max_depth,
                   filter_agent=filter_agent, show_full=show_full)


if __name__ == "__main__":
    main()
