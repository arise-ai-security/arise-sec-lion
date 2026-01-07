"""
Fetch agent hierarchy and generate a ReactFlow React component.

Nodes are color-coded by role:
  - BOSS: purple (#7c3aed)
  - MANAGER: blue (#2563eb)
  - WORKER: green (#16a34a)
  - PENDING: yellow (#eab308)

Features:
  - Status emojis on nodes: ✅ completed, ❌ failed, 🔄 in_progress, ⏳ pending, etc.
  - Click on any node to view detailed agent information in a modal
  - Shows: Objective, Complexity Evaluation, Budget, Configuration, Result/Error
  - Worker reason badge: shows why agent became WORKER (LLM Evaluation, Low Budget, Random Shortcut)
  - Subtasks include supervisor justifications (objective, plan, split reason, etc.)
  - Budget info: allocated, spent, remaining amounts per node
  - Worker reports: shows detailed work information for each worker
    * Approach: how the worker tackled the task
    * 🔍 Observations: key discoveries and findings during execution
    * 💭 Worker's Reasoning: worker's own justification (not supervisor's guidance)
    * 📦 Deliverables: specific file changes with descriptions (e.g., "auth.py: added JWT logic")
    * ✅ Fulfillment Evidence: concrete examples of meeting supervisor expectations
    * ⚠️ Challenges: issues encountered and how they were addressed
  - Aggregated summary: for MANAGER/BOSS nodes, shows combined deliverables and approaches from all subordinates
  - Child worker reports: accumulated reports from all workers in the subtree
  - Context sharing (consistent with web UI):
    * Published context (BOSS/MANAGER): Shows context published to dashboard
      - Source context (from original prompt): bug_summary, error_messages, reproduction_steps,
        file_paths, commit_references, urls, environment, dependencies, key_facts
      - CWE Pattern Analysis: inferred_cwes, cwe_reasoning, cwe_confidence, fix_patterns, recommended_sanitizers
      - Worker context: objective, justification, work_analysis, tags
    * Inherited context (WORKER): Shows knowledge inherited from dashboard
      - Source context: extracted key information from original prompt
      - CWE information: inferred CWEs and fix patterns passed down to workers
      - Worker context: previously completed work relevant to current task
  - Color-coded nodes by role
  - Automatic tree layout

Usage:
  python3 scripts/generate_reactflow.py              # Use latest agent
  python3 scripts/generate_reactflow.py --list       # List all available agents
  python3 scripts/generate_reactflow.py <agent-id>   # Use specific agent ID
  python3 scripts/generate_reactflow.py -n 2         # Use the 3rd most recent agent
"""

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "http://localhost:8000/api"

NODE_WIDTH = 200
HORIZONTAL_SPACING = 50
NODE_HEIGHT = 80
VERTICAL_SPACING = 100

# Color mapping by role
ROLE_COLORS = {
    "boss": "#7c3aed",  # purple
    "manager": "#2563eb",  # blue
    "worker": "#16a34a",  # green
    "pending": "#eab308",  # yellow
}

# Status emoji mapping
STATUS_EMOJIS = {
    "pending": "⏳",
    "analyzing": "🔍",
    "in_progress": "🔄",
    "waiting": "⏸️",
    "completed": "✅",
    "failed": "❌",
    "blocked": "🚫",
    "terminated": "⛔",
    "verifying": "🔬",
}


def curl_get(url: str, timeout: int = 5, exit_on_error: bool = False) -> dict | list:
    """Execute curl and return JSON response."""
    result = subprocess.run(
        ["curl", "-s", "--max-time", str(timeout), url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if exit_on_error:
            print(f"Error: Could not connect to {url}", file=sys.stderr)
            print("Make sure the API server is running at localhost:8000", file=sys.stderr)
            sys.exit(1)
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        if exit_on_error:
            print(f"Error: Invalid JSON response from {url}", file=sys.stderr)
            sys.exit(1)
        return {}


def list_agents() -> list:
    """Fetch and return list of all agents."""
    result = curl_get(f"{API_BASE}/agents", timeout=10, exit_on_error=True)
    return result if isinstance(result, list) else []


def get_hierarchy(agent_id: str) -> dict:
    """Fetch the hierarchy for a given agent ID."""
    result = curl_get(f"{API_BASE}/agents/{agent_id}/hierarchy", timeout=10, exit_on_error=True)
    return result if isinstance(result, dict) else {}


def get_agent_summary(agent_id: str) -> dict:
    """Fetch the summary for a given agent ID (5s timeout, silent fail)."""
    result = curl_get(f"{API_BASE}/agents/{agent_id}/summary", timeout=5)
    return result if isinstance(result, dict) else {}


def truncate_label(text: str, max_len: int = 35) -> str:
    """Truncate text with ellipsis if too long."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def collect_agent_ids(node: dict) -> list[str]:
    """Recursively collect all agent IDs from the hierarchy."""
    ids = [node["id"]]
    for child in node.get("children", []):
        ids.extend(collect_agent_ids(child))
    return ids


def fetch_all_summaries(agent_ids: list[str], max_workers: int = 20) -> dict[str, dict]:
    """Fetch summaries for all agents in parallel."""
    summaries = {}
    total = len(agent_ids)
    completed = 0

    def fetch_one(agent_id: str) -> tuple[str, dict]:
        return agent_id, get_agent_summary(agent_id)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, aid): aid for aid in agent_ids}
        for future in as_completed(futures):
            agent_id, summary = future.result()
            if summary:
                summaries[agent_id] = summary
            completed += 1
            print(
                f"  Fetching summaries: {completed}/{total}...",
                file=sys.stderr,
                end="\r",
            )

    print(f"  Fetched {len(summaries)} summaries.       ", file=sys.stderr)
    return summaries


def layout_tree(
    node: dict,
    depth: int,
    offset: int,
    nodes: list,
    edges: list,
    summaries: dict[str, dict],
) -> int:
    """
    Recursively layout tree nodes.
    Returns width (number of leaf slots used).
    """
    children = node.get("children", [])
    role = node["role"]
    status = node["status"]
    color = ROLE_COLORS.get(role, "#6b7280")  # gray fallback

    # Get summary data for this agent
    summary = summaries.get(node["id"], {})

    # Get status emoji
    status_emoji = STATUS_EMOJIS.get(status, "❓")

    # Build node data with full metadata
    node_data = {
        "label": f"{status_emoji} {role.upper()}\n{truncate_label(node['task_description'])}",
        "role": role,
        "status": status,
        "statusEmoji": status_emoji,
        # Objective
        "taskDescription": node["task_description"],
        # Complexity evaluation
        "complexity": summary.get("complexity"),
        "complexityReasoning": summary.get("complexity_reasoning"),
        # Configuration
        "configStrategy": summary.get("config_strategy"),
        "configDetails": summary.get("config_details", {}),
        "workerTool": summary.get("worker_tool"),
        # Result / Error
        "result": summary.get("result"),
        "errorMessage": summary.get("error_message"),
        # Subtasks (for managers)
        "subtasks": summary.get("subtasks", []),
        # Budget info
        "budget": summary.get("budget"),
        # Worker report (for workers)
        "workerReport": summary.get("worker_report"),
        # Child worker reports (for managers/boss)
        "childWorkerReports": summary.get("child_worker_reports", []),
        # Aggregated summary (for managers/boss)
        "aggregatedSummary": summary.get("aggregated_summary"),
        # Context sharing - published by supervisor (for BOSS/MANAGER)
        "publishedContext": summary.get("published_context", []),
        # Context sharing - inherited by worker (for WORKER)
        "inheritedContext": summary.get("inherited_context"),
        # Supervisor's justification (for non-BOSS agents - received from parent)
        "supervisorJustification": summary.get("supervisor_justification"),
        # Additional info
        "childrenCount": len(children),
        "depth": depth,
    }

    if not children:
        # Leaf node - place at current offset
        nodes.append(
            {
                "id": node["id"],
                "position": {
                    "x": offset * (NODE_WIDTH + HORIZONTAL_SPACING),
                    "y": depth * (NODE_HEIGHT + VERTICAL_SPACING),
                },
                "data": node_data,
                "style": {
                    "background": color,
                    "color": "white",
                    "border": "none",
                    "borderRadius": "8px",
                    "padding": "10px",
                    "fontSize": "11px",
                    "width": NODE_WIDTH,
                    "cursor": "pointer",
                },
            }
        )
        return 1

    # Layout children first
    children_width = 0
    for child in children:
        edge_id = f"e-{node['id'][:8]}-{child['id'][:8]}"
        edges.append(
            {
                "id": edge_id,
                "source": node["id"],
                "target": child["id"],
                "type": "smoothstep",
            }
        )
        children_width += layout_tree(
            child, depth + 1, offset + children_width, nodes, edges, summaries
        )

    # Place parent centered above children
    parent_x = offset + (children_width - 1) / 2
    nodes.append(
        {
            "id": node["id"],
            "position": {
                "x": parent_x * (NODE_WIDTH + HORIZONTAL_SPACING),
                "y": depth * (NODE_HEIGHT + VERTICAL_SPACING),
            },
            "data": node_data,
            "style": {
                "background": color,
                "color": "white",
                "border": "none",
                "borderRadius": "8px",
                "padding": "10px",
                "fontSize": "11px",
                "width": NODE_WIDTH,
                "cursor": "pointer",
            },
        }
    )

    return children_width


def generate_react_component(nodes: list, edges: list) -> str:
    """Generate the ReactFlow component code with modal support."""
    # Format nodes as JSON with indentation
    nodes_json = json.dumps(nodes, indent=2)
    edges_json = json.dumps(edges, indent=2)

    # Build component using string concatenation to avoid f-string brace issues
    component = """import React, { useCallback, useState } from 'react';
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  addEdge,
} from 'reactflow';
import 'reactflow/dist/style.css';

// Color legend:
// BOSS: purple (#7c3aed)
// MANAGER: blue (#2563eb)
// WORKER: green (#16a34a)
// PENDING: yellow (#eab308)

const ROLE_COLORS = {
  boss: '#7c3aed',
  manager: '#2563eb',
  worker: '#16a34a',
  pending: '#eab308',
};

const STATUS_BADGES = {
  pending: { bg: '#fef3c7', text: '#92400e' },
  analyzing: { bg: '#dbeafe', text: '#1e40af' },
  in_progress: { bg: '#d1fae5', text: '#065f46' },
  waiting: { bg: '#e0e7ff', text: '#3730a3' },
  completed: { bg: '#d1fae5', text: '#065f46' },
  failed: { bg: '#fee2e2', text: '#991b1b' },
  blocked: { bg: '#fecaca', text: '#991b1b' },
  terminated: { bg: '#e5e7eb', text: '#374151' },
  verifying: { bg: '#fae8ff', text: '#86198f' },
};

const COMPLEXITY_COLORS = {
  simple: { bg: '#d1fae5', text: '#065f46' },
  complex: { bg: '#fee2e2', text: '#991b1b' },
};

// Worker assignment reason detection
const WORKER_REASONS = {
  low_budget: { label: 'Low Budget', bg: '#fef3c7', text: '#92400e', icon: '💰' },
  random: { label: 'Random Shortcut', bg: '#e0e7ff', text: '#3730a3', icon: '🎲' },
  llm_evaluation: { label: 'LLM Evaluation', bg: '#d1fae5', text: '#065f46', icon: '🤖' },
};

function getWorkerReason(complexityReasoning) {
  if (!complexityReasoning) return null;
  const lower = complexityReasoning.toLowerCase();
  if (lower.includes('shortcut') && lower.includes('budget')) {
    return 'low_budget';
  }
  if (lower.includes('shortcut') && lower.includes('random')) {
    return 'random';
  }
  // If it has reasoning but not a shortcut, it's from LLM evaluation
  return 'llm_evaluation';
}

// Helper to check if a field has real content (not legacy placeholder)
function hasRealContent(value) {
  return value && value !== '(legacy event)' && value.trim() !== '';
}

// Section component for the modal
function Section({ title, children }) {
  if (!children) return null;
  return (
    <div style={{ marginBottom: '20px' }}>
      <h3 style={{ fontSize: '13px', fontWeight: '600', color: '#6b7280', marginBottom: '8px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
        {title}
      </h3>
      {children}
    </div>
  );
}

// Modal component for agent details
function AgentModal({ agent, onClose }) {
  if (!agent) return null;

  const roleColor = ROLE_COLORS[agent.role] || '#6b7280';
  const statusStyle = STATUS_BADGES[agent.status] || { bg: '#e5e7eb', text: '#374151' };
  const complexityStyle = agent.complexity ? (COMPLEXITY_COLORS[agent.complexity] || { bg: '#e5e7eb', text: '#374151' }) : null;

  // For WORKER agents, determine why they became a worker
  const workerReasonKey = agent.role === 'worker' ? getWorkerReason(agent.complexityReasoning) : null;
  const workerReason = workerReasonKey ? WORKER_REASONS[workerReasonKey] : null;

  const hasConfig = agent.configStrategy || agent.workerTool || (agent.configDetails && Object.keys(agent.configDetails).length > 0);

  return (
    <div
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        backgroundColor: 'rgba(0, 0, 0, 0.5)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
      onClick={onClose}
    >
      <div
        style={{
          backgroundColor: 'white',
          borderRadius: '12px',
          padding: '24px',
          maxWidth: '650px',
          width: '90%',
          maxHeight: '85vh',
          overflow: 'auto',
          boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.25)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
            <span
              style={{
                backgroundColor: roleColor,
                color: 'white',
                padding: '4px 12px',
                borderRadius: '9999px',
                fontSize: '13px',
                fontWeight: '600',
                textTransform: 'uppercase',
              }}
            >
              {agent.role}
            </span>
            <span
              style={{
                backgroundColor: statusStyle.bg,
                color: statusStyle.text,
                padding: '4px 12px',
                borderRadius: '9999px',
                fontSize: '13px',
                fontWeight: '500',
              }}
            >
              {agent.statusEmoji} {agent.status}
            </span>
            {complexityStyle && (
              <span
                style={{
                  backgroundColor: complexityStyle.bg,
                  color: complexityStyle.text,
                  padding: '4px 12px',
                  borderRadius: '9999px',
                  fontSize: '13px',
                  fontWeight: '500',
                }}
              >
                {agent.complexity}
              </span>
            )}
            {workerReason && (
              <span
                style={{
                  backgroundColor: workerReason.bg,
                  color: workerReason.text,
                  padding: '4px 12px',
                  borderRadius: '9999px',
                  fontSize: '13px',
                  fontWeight: '500',
                }}
              >
                {workerReason.icon} {workerReason.label}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            style={{
              background: 'none',
              border: 'none',
              fontSize: '24px',
              cursor: 'pointer',
              color: '#6b7280',
              padding: '0',
              lineHeight: '1',
            }}
          >
            &times;
          </button>
        </div>

        {/* Objective */}
        <Section title="Objective">
          <p style={{ fontSize: '15px', color: '#1f2937', lineHeight: '1.6', margin: 0 }}>
            {agent.taskDescription}
          </p>
        </Section>

        {/* Supervisor's Assignment (for non-BOSS agents) - ABLATION STUDY: NOT INJECTED INTO PROMPT */}
        {agent.supervisorJustification && hasRealContent(agent.supervisorJustification.objective) && (
          <Section title="📋 Supervisor's Assignment">
            {/* EXPERIMENT MODE WARNING BANNER */}
            <div style={{ backgroundColor: '#fee2e2', padding: '10px', borderRadius: '6px', marginBottom: '10px', border: '2px solid #ef4444' }}>
              <div style={{ fontSize: '13px', fontWeight: '700', color: '#b91c1c', textAlign: 'center' }}>
                🚫 EXPERIMENT MODE 🚫
              </div>
              <div style={{ fontSize: '12px', fontWeight: '600', color: '#dc2626', textAlign: 'center', marginTop: '3px' }}>
                SUPERVISOR JUSTIFICATION DISABLED
              </div>
              <div style={{ fontSize: '11px', color: '#dc2626', textAlign: 'center', marginTop: '6px', fontStyle: 'italic' }}>
                The context below was generated by thinkers but NOT injected into the subagent's prompt.
                <br />
                Subagent executed WITHOUT access to this supervisor justification.
              </div>
            </div>
            <div style={{ backgroundColor: '#eef2ff', padding: '12px', borderRadius: '8px', borderLeft: '3px solid #6366f1', opacity: 0.6 }}>
              <div style={{ fontSize: '12px', color: '#6b7280', marginBottom: '8px', textDecoration: 'line-through' }}>
                This task was assigned by a supervisor with the following context:
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '13px' }}>
                {hasRealContent(agent.supervisorJustification.parent_task) && (
                  <div>
                    <span style={{ fontWeight: '500', color: '#4b5563', textDecoration: 'line-through' }}>Parent's Task:</span>
                    <span style={{ color: '#374151', marginLeft: '6px' }}>{agent.supervisorJustification.parent_task}</span>
                  </div>
                )}
                {hasRealContent(agent.supervisorJustification.objective) && (
                  <div>
                    <span style={{ fontWeight: '500', color: '#4b5563', textDecoration: 'line-through' }}>Expected Objective:</span>
                    <span style={{ color: '#374151', marginLeft: '6px' }}>{agent.supervisorJustification.objective}</span>
                  </div>
                )}
                {hasRealContent(agent.supervisorJustification.split_reason) && (
                  <div>
                    <span style={{ fontWeight: '500', color: '#4b5563', textDecoration: 'line-through' }}>Why Assigned:</span>
                    <span style={{ color: '#374151', marginLeft: '6px' }}>{agent.supervisorJustification.split_reason}</span>
                  </div>
                )}
                {hasRealContent(agent.supervisorJustification.plan) && (
                  <div>
                    <span style={{ fontWeight: '500', color: '#4b5563', textDecoration: 'line-through' }}>Suggested Approach:</span>
                    <span style={{ color: '#374151', marginLeft: '6px' }}>{agent.supervisorJustification.plan}</span>
                  </div>
                )}
                {hasRealContent(agent.supervisorJustification.why_it_may_work) && (
                  <div>
                    <span style={{ fontWeight: '500', color: '#4b5563', textDecoration: 'line-through' }}>Why It May Work:</span>
                    <span style={{ color: '#374151', marginLeft: '6px' }}>{agent.supervisorJustification.why_it_may_work}</span>
                  </div>
                )}
                {hasRealContent(agent.supervisorJustification.expected_results) && (
                  <div>
                    <span style={{ fontWeight: '500', color: '#4b5563', textDecoration: 'line-through' }}>Expected Deliverables:</span>
                    <span style={{ color: '#374151', marginLeft: '6px' }}>{agent.supervisorJustification.expected_results}</span>
                  </div>
                )}
                {/* Budget Allocation Context */}
                {hasRealContent(agent.supervisorJustification.budget_allocation) && (
                  <div style={{ marginTop: '8px', paddingTop: '8px', borderTop: '1px solid #c7d2fe' }}>
                    <div style={{ fontWeight: '500', color: '#7c3aed', textDecoration: 'line-through', marginBottom: '4px' }}>Budget & Priority Context:</div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', fontSize: '12px' }}>
                      <div><span style={{ color: '#7c3aed', textDecoration: 'line-through' }}>Budget Allocation:</span> <span style={{ color: '#374151' }}>{agent.supervisorJustification.budget_allocation}</span></div>
                      {hasRealContent(agent.supervisorJustification.complexity_assessment) && (
                        <div><span style={{ color: '#7c3aed', textDecoration: 'line-through' }}>Complexity:</span> <span style={{ color: '#374151' }}>{agent.supervisorJustification.complexity_assessment}</span></div>
                      )}
                      {hasRealContent(agent.supervisorJustification.significance_weight) && (
                        <div><span style={{ color: '#7c3aed', textDecoration: 'line-through' }}>Significance:</span> <span style={{ color: '#374151' }}>{agent.supervisorJustification.significance_weight}</span></div>
                      )}
                      {hasRealContent(agent.supervisorJustification.resource_justification) && (
                        <div><span style={{ color: '#7c3aed', textDecoration: 'line-through' }}>Resource Justification:</span> <span style={{ color: '#374151' }}>{agent.supervisorJustification.resource_justification}</span></div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </Section>
        )}

        {/* Complexity Evaluation */}
        {agent.complexityReasoning && (
          <Section title="Complexity Evaluation">
            <div style={{ backgroundColor: '#f9fafb', borderRadius: '8px', padding: '12px' }}>
              <p style={{ fontSize: '14px', color: '#374151', lineHeight: '1.6', margin: 0 }}>
                {agent.complexityReasoning}
              </p>
            </div>
          </Section>
        )}

        {/* Budget */}
        {agent.budget && (
          <Section title="Budget">
            <div style={{ backgroundColor: '#f0fdf4', borderRadius: '8px', padding: '12px', border: '1px solid #bbf7d0' }}>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '12px' }}>
                <div>
                  <div style={{ fontSize: '11px', color: '#6b7280', marginBottom: '2px' }}>Allocated</div>
                  <div style={{ fontSize: '16px', fontWeight: '600', color: '#16a34a' }}>${agent.budget.initial_budget?.toFixed(2) || '0.00'}</div>
                </div>
                <div>
                  <div style={{ fontSize: '11px', color: '#6b7280', marginBottom: '2px' }}>Spent</div>
                  <div style={{ fontSize: '16px', fontWeight: '600', color: '#dc2626' }}>${agent.budget.spent?.toFixed(2) || '0.00'}</div>
                </div>
                <div>
                  <div style={{ fontSize: '11px', color: '#6b7280', marginBottom: '2px' }}>Remaining</div>
                  <div style={{ fontSize: '16px', fontWeight: '600', color: '#2563eb' }}>${agent.budget.current_budget?.toFixed(2) || '0.00'}</div>
                </div>
              </div>
              {agent.budget.source && (
                <div style={{ marginTop: '8px', fontSize: '11px', color: '#6b7280' }}>
                  Source: <span style={{ fontWeight: '500' }}>{agent.budget.source}</span>
                </div>
              )}
            </div>
          </Section>
        )}

        {/* Configuration */}
        {hasConfig && (
          <Section title="Configuration">
            <div style={{ backgroundColor: '#f9fafb', borderRadius: '8px', padding: '12px' }}>
              {agent.configStrategy && (
                <div style={{ marginBottom: agent.workerTool || Object.keys(agent.configDetails || {}).length > 0 ? '12px' : 0 }}>
                  <div style={{ fontSize: '12px', color: '#6b7280', marginBottom: '4px' }}>Strategy</div>
                  <div style={{ fontSize: '14px', color: '#1f2937' }}>{agent.configStrategy}</div>
                </div>
              )}
              {agent.workerTool && (
                <div style={{ marginBottom: Object.keys(agent.configDetails || {}).length > 0 ? '12px' : 0 }}>
                  <div style={{ fontSize: '12px', color: '#6b7280', marginBottom: '4px' }}>Worker Tool</div>
                  <div style={{ fontSize: '14px', color: '#1f2937', display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <span>{agent.workerTool === 'claude_code' ? '🤖' : '🔧'}</span>
                    <span>{agent.workerTool === 'claude_code' ? 'Claude Code' : 'OpenHands'}</span>
                  </div>
                </div>
              )}
              {agent.configDetails && Object.keys(agent.configDetails).length > 0 && (
                <div>
                  <div style={{ fontSize: '12px', color: '#6b7280', marginBottom: '4px' }}>Details</div>
                  <pre style={{ fontSize: '12px', color: '#1f2937', margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {JSON.stringify(agent.configDetails, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          </Section>
        )}

        {/* Subtasks (for managers) */}
        {agent.subtasks && agent.subtasks.length > 0 && (
          <Section title="Subtasks">
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {agent.subtasks.map((subtask, index) => {
                const j = subtask.justification || {};
                const hasJustification = hasRealContent(j.objective) || hasRealContent(j.plan) || hasRealContent(j.split_reason) || hasRealContent(j.why_it_may_work) || hasRealContent(j.expected_results) || hasRealContent(j.budget_allocation);
                return (
                  <div
                    key={index}
                    style={{
                      backgroundColor: '#f9fafb',
                      borderRadius: '8px',
                      padding: '12px',
                      border: '1px solid #e5e7eb',
                    }}
                  >
                    {/* Header with description, budget weight, and status */}
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '12px', marginBottom: hasJustification ? '10px' : 0 }}>
                      <span style={{ fontSize: '14px', color: '#1f2937', fontWeight: '500', flex: 1 }}>{subtask.description}</span>
                      <div style={{ display: 'flex', gap: '6px', alignItems: 'center', flexShrink: 0 }}>
                        {subtask.budget_weight != null && subtask.budget_weight !== 1.0 && (
                          <span
                            style={{
                              backgroundColor: '#dbeafe',
                              color: '#1e40af',
                              padding: '2px 8px',
                              borderRadius: '9999px',
                              fontSize: '11px',
                              fontWeight: '500',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {(subtask.budget_weight * 100).toFixed(0)}% budget
                          </span>
                        )}
                        {subtask.child_status && (
                          <span
                            style={{
                              backgroundColor: (STATUS_BADGES[subtask.child_status] || { bg: '#e5e7eb' }).bg,
                              color: (STATUS_BADGES[subtask.child_status] || { text: '#374151' }).text,
                              padding: '2px 8px',
                              borderRadius: '9999px',
                              fontSize: '11px',
                              fontWeight: '500',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {subtask.child_status}
                          </span>
                        )}
                      </div>
                    </div>
                    {/* Supervisor Justification - ABLATION STUDY: NOT INJECTED INTO SUBAGENT PROMPTS */}
                    {hasJustification && (
                      <div style={{ borderTop: '1px solid #e5e7eb', paddingTop: '10px' }}>
                        {/* EXPERIMENT MODE WARNING BANNER */}
                        <div style={{ backgroundColor: '#fee2e2', padding: '8px', borderRadius: '6px', marginBottom: '8px', border: '2px solid #ef4444' }}>
                          <div style={{ fontSize: '12px', fontWeight: '700', color: '#b91c1c', textAlign: 'center' }}>
                            🚫 EXPERIMENT MODE 🚫
                          </div>
                          <div style={{ fontSize: '11px', fontWeight: '600', color: '#dc2626', textAlign: 'center', marginTop: '2px' }}>
                            SUPERVISOR JUSTIFICATION DISABLED
                          </div>
                          <div style={{ fontSize: '10px', color: '#dc2626', textAlign: 'center', marginTop: '4px', fontStyle: 'italic' }}>
                            The context below was generated by thinkers but NOT injected into the subagent's prompt.
                            <br />
                            Subagent executed WITHOUT access to this supervisor justification.
                          </div>
                        </div>
                        <div style={{ fontSize: '12px', color: '#6b7280', display: 'flex', flexDirection: 'column', gap: '6px', opacity: 0.6 }}>
                          {hasRealContent(j.objective) && (
                            <div><span style={{ fontWeight: '500', color: '#4b5563', textDecoration: 'line-through' }}>Objective:</span> {j.objective}</div>
                          )}
                          {hasRealContent(j.plan) && (
                            <div><span style={{ fontWeight: '500', color: '#4b5563', textDecoration: 'line-through' }}>Plan:</span> {j.plan}</div>
                          )}
                          {hasRealContent(j.split_reason) && (
                            <div><span style={{ fontWeight: '500', color: '#4b5563', textDecoration: 'line-through' }}>Split Reason:</span> {j.split_reason}</div>
                          )}
                          {hasRealContent(j.why_it_may_work) && (
                            <div><span style={{ fontWeight: '500', color: '#4b5563', textDecoration: 'line-through' }}>Why It May Work:</span> {j.why_it_may_work}</div>
                          )}
                          {hasRealContent(j.expected_results) && (
                            <div><span style={{ fontWeight: '500', color: '#4b5563', textDecoration: 'line-through' }}>Expected Results:</span> {j.expected_results}</div>
                          )}
                          {/* Budget Allocation Reasoning */}
                          {hasRealContent(j.budget_allocation) && (
                            <div style={{ marginTop: '8px', paddingTop: '8px', borderTop: '1px solid #e9d5ff' }}>
                              <span style={{ fontWeight: '500', color: '#7c3aed', textDecoration: 'line-through' }}>Budget Allocation:</span> {j.budget_allocation}
                            </div>
                          )}
                          {hasRealContent(j.complexity_assessment) && (
                            <div><span style={{ fontWeight: '500', color: '#7c3aed', textDecoration: 'line-through' }}>Complexity:</span> {j.complexity_assessment}</div>
                          )}
                          {hasRealContent(j.significance_weight) && (
                            <div><span style={{ fontWeight: '500', color: '#7c3aed', textDecoration: 'line-through' }}>Significance:</span> {j.significance_weight}</div>
                          )}
                          {hasRealContent(j.resource_justification) && (
                            <div><span style={{ fontWeight: '500', color: '#7c3aed', textDecoration: 'line-through' }}>Resource Justification:</span> {j.resource_justification}</div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </Section>
        )}

        {/* Worker Report (for workers) */}
        {agent.workerReport && (
          <Section title="Worker Report">
            <div style={{ backgroundColor: '#f0fdf4', borderRadius: '8px', padding: '12px', border: '1px solid #bbf7d0' }}>
              <div style={{ fontSize: '12px', color: '#6b7280', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {agent.workerReport.approach && agent.workerReport.approach !== 'Executed task using available tools' && (
                  <div><span style={{ fontWeight: '500', color: '#4b5563' }}>Approach:</span> {agent.workerReport.approach}</div>
                )}
                {agent.workerReport.observations && agent.workerReport.observations !== 'Task executed as planned' && (
                  <div><span style={{ fontWeight: '500', color: '#2563eb' }}>🔍 Observations:</span> {agent.workerReport.observations}</div>
                )}
                {agent.workerReport.reasoning && !agent.workerReport.reasoning.includes('(legacy event)') && agent.workerReport.reasoning !== 'Followed standard execution approach for the given task' && agent.workerReport.reasoning !== 'Task executed using standard approach with successful completion' && (
                  <div><span style={{ fontWeight: '500', color: '#4b5563' }}>💭 Worker's Reasoning:</span> {agent.workerReport.reasoning}</div>
                )}
                {agent.workerReport.deliverables && agent.workerReport.deliverables !== 'Task completed' && (
                  <div>
                    <span style={{ fontWeight: '500', color: '#4b5563' }}>📦 Deliverables:</span>
                    {agent.workerReport.deliverables.startsWith('Changes made:') ? (
                      <ul style={{ margin: '4px 0 0 0', paddingLeft: '16px' }}>
                        {agent.workerReport.deliverables.replace('Changes made: ', '').split('; ').map((change, idx) => {
                          const colonIdx = change.indexOf(':');
                          if (colonIdx > 0) {
                            const file = change.substring(0, colonIdx);
                            const desc = change.substring(colonIdx + 1).trim();
                            return (
                              <li key={idx} style={{ marginBottom: '2px' }}>
                                <code style={{ backgroundColor: '#dbeafe', padding: '1px 4px', borderRadius: '3px', fontFamily: 'monospace', color: '#1e40af', fontSize: '11px' }}>{file}</code>
                                <span style={{ marginLeft: '6px' }}>{desc}</span>
                              </li>
                            );
                          }
                          return <li key={idx} style={{ fontFamily: 'monospace' }}>{change}</li>;
                        })}
                      </ul>
                    ) : (
                      <span style={{ fontFamily: 'monospace' }}> {agent.workerReport.deliverables}</span>
                    )}
                  </div>
                )}
                {agent.workerReport.fulfillment_evidence && agent.workerReport.fulfillment_evidence !== 'Task completed successfully per assignment' && (
                  <div><span style={{ fontWeight: '500', color: '#7c3aed' }}>✅ Fulfillment Evidence:</span> {agent.workerReport.fulfillment_evidence}</div>
                )}
                {agent.workerReport.challenges && agent.workerReport.challenges !== 'No significant challenges encountered' && (
                  <div><span style={{ fontWeight: '500', color: '#ea580c' }}>⚠️ Challenges:</span> {agent.workerReport.challenges}</div>
                )}
              </div>
            </div>
          </Section>
        )}

        {/* Aggregated Summary (for managers/boss) */}
        {agent.aggregatedSummary && (
          <Section title="Work Summary">
            <div style={{ backgroundColor: '#faf5ff', borderRadius: '8px', padding: '12px', border: '1px solid #e9d5ff' }}>
              {/* Worker Statistics */}
              <div style={{ display: 'flex', gap: '16px', marginBottom: '12px', fontSize: '14px', flexWrap: 'wrap' }}>
                <div>
                  <span style={{ fontWeight: '600', color: '#7c3aed' }}>{agent.aggregatedSummary.total_workers}</span>
                  <span style={{ color: '#6b7280', marginLeft: '4px' }}>workers</span>
                </div>
                <div>
                  <span style={{ fontWeight: '600', color: '#16a34a' }}>{agent.aggregatedSummary.completed_workers}</span>
                  <span style={{ color: '#6b7280', marginLeft: '4px' }}>completed</span>
                </div>
                {agent.aggregatedSummary.failed_workers > 0 && (
                  <div>
                    <span style={{ fontWeight: '600', color: '#dc2626' }}>{agent.aggregatedSummary.failed_workers}</span>
                    <span style={{ color: '#6b7280', marginLeft: '4px' }}>failed</span>
                  </div>
                )}
              </div>
              {/* Combined Deliverables (includes tools used header) */}
              {agent.aggregatedSummary.combined_deliverables && (
                <div style={{ marginBottom: '10px' }}>
                  <div style={{ fontSize: '12px', fontWeight: '500', color: '#7c3aed', marginBottom: '4px' }}>Deliverables & Results:</div>
                  <div style={{ fontSize: '12px', color: '#374151', whiteSpace: 'pre-wrap', backgroundColor: 'white', padding: '10px', borderRadius: '4px', maxHeight: '250px', overflow: 'auto', lineHeight: '1.5', fontFamily: 'ui-monospace, monospace' }}>
                    {agent.aggregatedSummary.combined_deliverables}
                  </div>
                </div>
              )}
              {/* Combined Approach */}
              {agent.aggregatedSummary.combined_approach && (
                <div style={{ marginBottom: '10px' }}>
                  <div style={{ fontSize: '12px', fontWeight: '500', color: '#7c3aed', marginBottom: '4px' }}>Approaches & Reasoning:</div>
                  <div style={{ fontSize: '12px', color: '#374151', whiteSpace: 'pre-wrap', backgroundColor: 'white', padding: '10px', borderRadius: '4px', maxHeight: '200px', overflow: 'auto', lineHeight: '1.5', fontFamily: 'ui-monospace, monospace' }}>
                    {agent.aggregatedSummary.combined_approach}
                  </div>
                </div>
              )}
              {/* Key Challenges */}
              {agent.aggregatedSummary.key_challenges && (
                <div>
                  <div style={{ fontSize: '12px', fontWeight: '500', color: '#ea580c', marginBottom: '4px' }}>Challenges Encountered:</div>
                  <div style={{ fontSize: '12px', color: '#374151', whiteSpace: 'pre-wrap', backgroundColor: 'white', padding: '10px', borderRadius: '4px', maxHeight: '150px', overflow: 'auto', lineHeight: '1.5', fontFamily: 'ui-monospace, monospace' }}>
                    {agent.aggregatedSummary.key_challenges}
                  </div>
                </div>
              )}
            </div>
          </Section>
        )}

        {/* Child Worker Reports (for managers/boss) */}
        {agent.childWorkerReports && agent.childWorkerReports.length > 0 && (
          <Section title={`Worker Reports (${agent.childWorkerReports.length})`}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', maxHeight: '300px', overflow: 'auto' }}>
              {agent.childWorkerReports.map((childReport, index) => (
                <div
                  key={index}
                  style={{
                    backgroundColor: '#eff6ff',
                    borderRadius: '8px',
                    padding: '10px',
                    border: '1px solid #bfdbfe',
                  }}
                >
                  {/* Header with agent ID and status */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span style={{ fontFamily: 'monospace', fontSize: '11px', backgroundColor: '#dbeafe', padding: '2px 6px', borderRadius: '4px', color: '#1e40af' }}>
                        {childReport.agent_id}
                      </span>
                      <span style={{ fontSize: '12px', color: '#6b7280', maxWidth: '200px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {childReport.task}
                      </span>
                    </div>
                    <span
                      style={{
                        backgroundColor: (STATUS_BADGES[childReport.status] || { bg: '#e5e7eb' }).bg,
                        color: (STATUS_BADGES[childReport.status] || { text: '#374151' }).text,
                        padding: '2px 8px',
                        borderRadius: '9999px',
                        fontSize: '10px',
                        fontWeight: '500',
                      }}
                    >
                      {childReport.status}
                    </span>
                  </div>
                  {/* Worker Report Details */}
                  {childReport.report && (
                    <div style={{ fontSize: '12px', color: '#6b7280', display: 'flex', flexDirection: 'column', gap: '4px', backgroundColor: 'white', padding: '8px', borderRadius: '4px' }}>
                      {childReport.report.approach && childReport.report.approach !== 'Executed task using available tools' && (
                        <div><span style={{ fontWeight: '500', color: '#16a34a' }}>Approach:</span> {childReport.report.approach}</div>
                      )}
                      {childReport.report.observations && childReport.report.observations !== 'Task executed as planned' && (
                        <div><span style={{ fontWeight: '500', color: '#2563eb' }}>🔍 Observations:</span> {childReport.report.observations}</div>
                      )}
                      {childReport.report.reasoning && !childReport.report.reasoning.includes('(legacy event)') && childReport.report.reasoning !== 'Followed standard execution approach for the given task' && childReport.report.reasoning !== 'Task executed using standard approach with successful completion' && (
                        <div><span style={{ fontWeight: '500', color: '#16a34a' }}>💭 Worker's Reasoning:</span> {childReport.report.reasoning}</div>
                      )}
                      {childReport.report.deliverables && childReport.report.deliverables !== 'Task completed' && (
                        <div>
                          <span style={{ fontWeight: '500', color: '#16a34a' }}>📦 Deliverables:</span>
                          {childReport.report.deliverables.startsWith('Changes made:') ? (
                            <ul style={{ margin: '4px 0 0 0', paddingLeft: '16px' }}>
                              {childReport.report.deliverables.replace('Changes made: ', '').split('; ').map((change, idx) => {
                                const colonIdx = change.indexOf(':');
                                if (colonIdx > 0) {
                                  const file = change.substring(0, colonIdx);
                                  const desc = change.substring(colonIdx + 1).trim();
                                  return (
                                    <li key={idx} style={{ marginBottom: '2px' }}>
                                      <code style={{ backgroundColor: '#dbeafe', padding: '1px 4px', borderRadius: '3px', fontFamily: 'monospace', color: '#1e40af', fontSize: '10px' }}>{file}</code>
                                      <span style={{ marginLeft: '6px' }}>{desc}</span>
                                    </li>
                                  );
                                }
                                return <li key={idx} style={{ fontFamily: 'monospace' }}>{change}</li>;
                              })}
                            </ul>
                          ) : (
                            <span style={{ fontFamily: 'monospace' }}> {childReport.report.deliverables}</span>
                          )}
                        </div>
                      )}
                      {childReport.report.fulfillment_evidence && childReport.report.fulfillment_evidence !== 'Task completed successfully per assignment' && (
                        <div><span style={{ fontWeight: '500', color: '#7c3aed' }}>✅ Fulfillment Evidence:</span> {childReport.report.fulfillment_evidence}</div>
                      )}
                      {childReport.report.challenges && childReport.report.challenges !== 'No significant challenges encountered' && (
                        <div><span style={{ fontWeight: '500', color: '#ea580c' }}>⚠️ Challenges:</span> {childReport.report.challenges}</div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </Section>
        )}

        {/* Result */}
        {agent.result && (
          <Section title="Result">
            <div style={{ backgroundColor: '#d1fae5', borderRadius: '8px', padding: '12px', borderLeft: '4px solid #16a34a' }}>
              <p style={{ fontSize: '14px', color: '#065f46', lineHeight: '1.6', margin: 0, whiteSpace: 'pre-wrap' }}>
                {agent.result}
              </p>
            </div>
          </Section>
        )}

        {/* Error */}
        {agent.errorMessage && (
          <Section title="Error">
            <div style={{ backgroundColor: '#fee2e2', borderRadius: '8px', padding: '12px', borderLeft: '4px solid #dc2626' }}>
              <p style={{ fontSize: '14px', color: '#991b1b', lineHeight: '1.6', margin: 0, whiteSpace: 'pre-wrap' }}>
                {agent.errorMessage}
              </p>
            </div>
          </Section>
        )}

        {/* Published Context (for BOSS/MANAGER - context they contributed to dashboard) */}
        {agent.publishedContext && agent.publishedContext.length > 0 && (
          <Section title="📤 Context Published to Dashboard">
            <div style={{ marginBottom: '8px' }}>
              <p style={{ fontSize: '12px', color: '#6b7280', margin: 0 }}>
                This supervisor published {agent.publishedContext.length} context{agent.publishedContext.length !== 1 ? 's' : ''} to the global knowledge dashboard for cross-session learning.
              </p>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {agent.publishedContext.map((ctx, index) => (
                <div
                  key={ctx.entry_id || index}
                  style={{
                    backgroundColor: ctx.entry_type === 'source' ? '#ecfeff' : '#faf5ff',
                    borderRadius: '8px',
                    padding: '12px',
                    borderLeft: ctx.entry_type === 'source' ? '4px solid #06b6d4' : '4px solid #a855f7',
                  }}
                >
                  {/* Key (work_title) */}
                  <div style={{ display: 'flex', alignItems: 'flex-start', gap: '8px', marginBottom: '10px' }}>
                    <span style={{ color: ctx.entry_type === 'source' ? '#06b6d4' : '#a855f7', fontSize: '14px' }}>
                      {ctx.entry_type === 'source' ? '📋' : '🔑'}
                    </span>
                    <div style={{ flex: 1 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <div style={{ fontSize: '11px', color: ctx.entry_type === 'source' ? '#0891b2' : '#7c3aed', fontWeight: '600', textTransform: 'uppercase', marginBottom: '2px' }}>
                          {ctx.entry_type === 'source' ? 'Source Context' : 'Key'}
                        </div>
                        {ctx.entry_type === 'source' && (
                          <span style={{ backgroundColor: '#cffafe', color: '#0891b2', padding: '2px 6px', borderRadius: '4px', fontSize: '10px' }}>From Original Prompt</span>
                        )}
                      </div>
                      <div style={{ fontSize: '14px', fontWeight: '500', color: '#1f2937' }}>{ctx.work_title}</div>
                    </div>
                  </div>

                  {/* Source Context Data (for source type entries) */}
                  {ctx.entry_type === 'source' && ctx.source_context && (
                    <div style={{ marginLeft: '22px', borderTop: '1px solid #a5f3fc', paddingTop: '10px' }}>
                      <div style={{ fontSize: '11px', color: '#0891b2', fontWeight: '600', textTransform: 'uppercase', marginBottom: '8px' }}>Extracted Key Information</div>
                      {/* Bug Summary */}
                      {ctx.source_context.bug_summary && (
                        <div style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '11px', fontWeight: '500', color: '#dc2626', marginBottom: '2px' }}>🐛 Bug/Issue Summary:</div>
                          <div style={{ fontSize: '12px', color: '#374151', backgroundColor: '#fef2f2', padding: '8px', borderRadius: '4px' }}>{ctx.source_context.bug_summary}</div>
                        </div>
                      )}
                      {/* Error Messages */}
                      {ctx.source_context.error_messages && ctx.source_context.error_messages.length > 0 && (
                        <div style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '11px', fontWeight: '500', color: '#ea580c', marginBottom: '2px' }}>⚠️ Error Messages:</div>
                          <ul style={{ fontSize: '12px', color: '#374151', backgroundColor: '#fff7ed', padding: '8px', borderRadius: '4px', margin: 0, paddingLeft: '20px', fontFamily: 'monospace' }}>
                            {ctx.source_context.error_messages.map((err, idx) => (
                              <li key={idx}>{err}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {/* Reproduction Steps */}
                      {ctx.source_context.reproduction_steps && (
                        <div style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '11px', fontWeight: '500', color: '#2563eb', marginBottom: '2px' }}>🔄 Reproduction Steps:</div>
                          <div style={{ fontSize: '12px', color: '#374151', whiteSpace: 'pre-wrap', backgroundColor: '#eff6ff', padding: '8px', borderRadius: '4px' }}>{ctx.source_context.reproduction_steps}</div>
                        </div>
                      )}
                      {/* File Paths */}
                      {ctx.source_context.file_paths && ctx.source_context.file_paths.length > 0 && (
                        <div style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '11px', fontWeight: '500', color: '#16a34a', marginBottom: '2px' }}>📁 Referenced Files:</div>
                          <ul style={{ fontSize: '12px', color: '#374151', backgroundColor: '#f0fdf4', padding: '8px', borderRadius: '4px', margin: 0, paddingLeft: '20px', fontFamily: 'monospace' }}>
                            {ctx.source_context.file_paths.map((path, idx) => (
                              <li key={idx}>{path}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {/* Commit References */}
                      {ctx.source_context.commit_references && ctx.source_context.commit_references.length > 0 && (
                        <div style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '11px', fontWeight: '500', color: '#7c3aed', marginBottom: '2px' }}>🔖 Commit/Version References:</div>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                            {ctx.source_context.commit_references.map((ref, idx) => (
                              <span key={idx} style={{ backgroundColor: '#f3e8ff', color: '#7c3aed', padding: '2px 8px', borderRadius: '4px', fontSize: '11px', fontFamily: 'monospace' }}>{ref}</span>
                            ))}
                          </div>
                        </div>
                      )}
                      {/* URLs */}
                      {ctx.source_context.urls && ctx.source_context.urls.length > 0 && (
                        <div style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '11px', fontWeight: '500', color: '#4f46e5', marginBottom: '2px' }}>🔗 Related URLs:</div>
                          <ul style={{ fontSize: '12px', color: '#374151', backgroundColor: '#eef2ff', padding: '8px', borderRadius: '4px', margin: 0, paddingLeft: '20px', fontFamily: 'monospace' }}>
                            {ctx.source_context.urls.map((url, idx) => (
                              <li key={idx} style={{ wordBreak: 'break-all' }}>{url}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {/* Environment */}
                      {ctx.source_context.environment && (
                        <div style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '11px', fontWeight: '500', color: '#6b7280', marginBottom: '2px' }}>💻 Environment:</div>
                          <div style={{ fontSize: '12px', color: '#374151' }}>{ctx.source_context.environment}</div>
                        </div>
                      )}
                      {/* Dependencies */}
                      {ctx.source_context.dependencies && ctx.source_context.dependencies.length > 0 && (
                        <div style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '11px', fontWeight: '500', color: '#6b7280', marginBottom: '2px' }}>📦 Dependencies:</div>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                            {ctx.source_context.dependencies.map((dep, idx) => (
                              <span key={idx} style={{ backgroundColor: '#f3f4f6', color: '#374151', padding: '2px 8px', borderRadius: '4px', fontSize: '11px', fontFamily: 'monospace' }}>{dep}</span>
                            ))}
                          </div>
                        </div>
                      )}
                      {/* Key Facts */}
                      {ctx.source_context.key_facts && ctx.source_context.key_facts.length > 0 && (
                        <div style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '11px', fontWeight: '500', color: '#ca8a04', marginBottom: '2px' }}>⭐ Key Facts & Requirements:</div>
                          <ul style={{ fontSize: '12px', color: '#374151', backgroundColor: '#fefce8', padding: '8px', borderRadius: '4px', margin: 0, paddingLeft: '20px' }}>
                            {ctx.source_context.key_facts.map((fact, idx) => (
                              <li key={idx}>{fact}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {/* CWE Information */}
                      {ctx.source_context.inferred_cwes && ctx.source_context.inferred_cwes.length > 0 && (
                        <div style={{ marginBottom: '8px', backgroundColor: '#fef2f2', padding: '12px', borderRadius: '6px', border: '1px solid #fecaca' }}>
                          <div style={{ fontSize: '11px', fontWeight: '600', color: '#dc2626', marginBottom: '8px', textTransform: 'uppercase' }}>🛡️ CWE Pattern Analysis</div>
                          {/* CWE IDs */}
                          <div style={{ marginBottom: '8px' }}>
                            <div style={{ fontSize: '11px', fontWeight: '500', color: '#991b1b', marginBottom: '4px' }}>Inferred CWEs:</div>
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                              {ctx.source_context.inferred_cwes.map((cwe, idx) => (
                                <span key={idx} style={{ backgroundColor: '#fee2e2', color: '#991b1b', padding: '4px 10px', borderRadius: '6px', fontSize: '12px', fontWeight: '600', fontFamily: 'monospace' }}>{cwe}</span>
                              ))}
                            </div>
                          </div>
                          {/* CWE Reasoning */}
                          {ctx.source_context.cwe_reasoning && Object.keys(ctx.source_context.cwe_reasoning).length > 0 && (
                            <div style={{ marginBottom: '8px' }}>
                              <div style={{ fontSize: '11px', fontWeight: '500', color: '#991b1b', marginBottom: '4px' }}>Analysis Reasoning:</div>
                              <div style={{ backgroundColor: 'white', padding: '8px', borderRadius: '4px', fontSize: '12px' }}>
                                {Object.entries(ctx.source_context.cwe_reasoning).map(([cwe, reason], idx) => (
                                  <div key={idx} style={{ marginBottom: idx < Object.keys(ctx.source_context.cwe_reasoning).length - 1 ? '6px' : 0 }}>
                                    <span style={{ fontWeight: '600', color: '#dc2626' }}>{cwe}:</span>
                                    <span style={{ color: '#374151', marginLeft: '6px' }}>{reason}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                          {/* CWE Confidence */}
                          {ctx.source_context.cwe_confidence && Object.keys(ctx.source_context.cwe_confidence).length > 0 && (
                            <div style={{ marginBottom: '8px' }}>
                              <div style={{ fontSize: '11px', fontWeight: '500', color: '#991b1b', marginBottom: '4px' }}>Confidence Levels:</div>
                              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                                {Object.entries(ctx.source_context.cwe_confidence).map(([cwe, conf], idx) => {
                                  const confColor = conf === 'high' ? '#16a34a' : conf === 'medium' ? '#ca8a04' : '#6b7280';
                                  return (
                                    <span key={idx} style={{ backgroundColor: '#f9fafb', padding: '3px 8px', borderRadius: '4px', fontSize: '11px' }}>
                                      <span style={{ fontFamily: 'monospace', fontWeight: '500' }}>{cwe}</span>
                                      <span style={{ marginLeft: '4px', color: confColor, fontWeight: '600' }}>{conf}</span>
                                    </span>
                                  );
                                })}
                              </div>
                            </div>
                          )}
                          {/* Fix Patterns */}
                          {ctx.source_context.fix_patterns && Object.keys(ctx.source_context.fix_patterns).length > 0 && (
                            <div style={{ marginBottom: '8px' }}>
                              <div style={{ fontSize: '11px', fontWeight: '500', color: '#16a34a', marginBottom: '4px' }}>🔧 Recommended Fix Patterns:</div>
                              <div style={{ backgroundColor: '#f0fdf4', padding: '8px', borderRadius: '4px', fontSize: '12px' }}>
                                {Object.entries(ctx.source_context.fix_patterns).map(([cwe, pattern], idx) => (
                                  <div key={idx} style={{ marginBottom: idx < Object.keys(ctx.source_context.fix_patterns).length - 1 ? '6px' : 0 }}>
                                    <span style={{ fontWeight: '600', color: '#15803d', fontFamily: 'monospace' }}>{cwe}:</span>
                                    <span style={{ color: '#374151', marginLeft: '6px' }}>{pattern}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                          {/* Recommended Sanitizers */}
                          {ctx.source_context.recommended_sanitizers && ctx.source_context.recommended_sanitizers.length > 0 && (
                            <div>
                              <div style={{ fontSize: '11px', fontWeight: '500', color: '#7c3aed', marginBottom: '4px' }}>🧪 Recommended Sanitizers:</div>
                              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                                {ctx.source_context.recommended_sanitizers.map((san, idx) => (
                                  <span key={idx} style={{ backgroundColor: '#f3e8ff', color: '#7c3aed', padding: '2px 8px', borderRadius: '4px', fontSize: '11px', fontFamily: 'monospace' }}>{san}</span>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>
                      )}
                      {/* Tags */}
                      {ctx.tags && ctx.tags.length > 0 && (
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginBottom: '8px' }}>
                          {ctx.tags.map((tag, tagIdx) => (
                            <span key={tagIdx} style={{ backgroundColor: '#cffafe', color: '#0891b2', padding: '2px 8px', borderRadius: '9999px', fontSize: '11px' }}>{tag}</span>
                          ))}
                        </div>
                      )}
                      {/* Metadata */}
                      <div style={{ paddingTop: '8px', borderTop: '1px solid #a5f3fc', fontSize: '11px', color: '#9ca3af' }}>
                        Extracted at: {ctx.published_at && new Date(ctx.published_at).toLocaleString()}
                      </div>
                    </div>
                  )}

                  {/* Worker Context Value Section (for worker type entries) */}
                  {ctx.entry_type === 'worker' && (
                    <div style={{ marginLeft: '22px', borderTop: '1px solid #e9d5ff', paddingTop: '10px' }}>
                      <div style={{ fontSize: '11px', color: '#7c3aed', fontWeight: '600', textTransform: 'uppercase', marginBottom: '8px' }}>Value</div>
                      {/* Objective */}
                      <div style={{ marginBottom: '8px' }}>
                        <div style={{ fontSize: '11px', fontWeight: '500', color: '#4b5563', marginBottom: '2px' }}>Objective:</div>
                        <div style={{ fontSize: '12px', color: '#374151' }}>{ctx.objective}</div>
                      </div>
                      {/* Justification */}
                      {ctx.justification && (
                        <div style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '11px', fontWeight: '500', color: '#4b5563', marginBottom: '2px' }}>Why Assigned:</div>
                          <div style={{ fontSize: '12px', color: '#374151' }}>{ctx.justification}</div>
                        </div>
                      )}
                      {/* Work Analysis */}
                      {ctx.work_analysis && (
                        <div style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '11px', fontWeight: '500', color: '#4b5563', marginBottom: '2px' }}>How It Was Accomplished:</div>
                          <div style={{ fontSize: '12px', color: '#374151', whiteSpace: 'pre-wrap', backgroundColor: 'rgba(255,255,255,0.5)', padding: '8px', borderRadius: '4px', maxHeight: '150px', overflow: 'auto' }}>
                            {ctx.work_analysis}
                          </div>
                        </div>
                      )}
                      {/* Tags */}
                      {ctx.tags && ctx.tags.length > 0 && (
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginBottom: '8px' }}>
                          {ctx.tags.map((tag, tagIdx) => (
                            <span
                              key={tagIdx}
                              style={{
                                backgroundColor: '#e9d5ff',
                                color: '#7c3aed',
                                padding: '2px 8px',
                                borderRadius: '9999px',
                                fontSize: '11px',
                              }}
                            >
                              {tag}
                            </span>
                          ))}
                        </div>
                      )}
                      {/* Metadata */}
                      <div style={{ display: 'flex', gap: '12px', paddingTop: '8px', borderTop: '1px solid #f3e8ff', fontSize: '11px', color: '#9ca3af' }}>
                        <span>Worker: {ctx.worker_id ? ctx.worker_id.substring(0, 8) + '...' : 'N/A'}</span>
                        {ctx.published_at && <span>{new Date(ctx.published_at).toLocaleString()}</span>}
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </Section>
        )}

        {/* Inherited Context (for WORKER - context they received from dashboard) */}
        {agent.role === 'worker' && (
          <Section title="📥 Inherited Knowledge from Dashboard">
            <div style={{ backgroundColor: '#ecfeff', borderRadius: '8px', padding: '12px', borderLeft: '4px solid #06b6d4' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
                <span style={{ fontSize: '18px' }}>🧠</span>
                <span style={{ fontSize: '14px', fontWeight: '500', color: '#374151' }}>Cross-Session Learning Active</span>
              </div>
              <p style={{ fontSize: '12px', color: '#4b5563', marginBottom: '12px' }}>
                This worker inherited knowledge from {agent.inheritedContext?.total_available || 0} available context entries in the global dashboard.
                Source context (from original prompt) is always included. Relevant worker context was automatically identified.
              </p>
              {/* Reminder Banner */}
              <div style={{ backgroundColor: '#fef3c7', padding: '10px', borderRadius: '6px', marginBottom: '12px', border: '1px solid #fcd34d' }}>
                <p style={{ fontSize: '12px', fontWeight: '500', color: '#92400e', marginBottom: '4px' }}>⚠️ Learning Reminders:</p>
                <ul style={{ fontSize: '11px', color: '#b45309', margin: 0, paddingLeft: '16px' }}>
                  <li>Do NOT repeat work that has already been completed</li>
                  <li>Avoid repeating the same mistakes encountered before</li>
                  <li>Build upon successful approaches from previous work</li>
                </ul>
              </div>
              {agent.inheritedContext?.entries && agent.inheritedContext.entries.length > 0 ? (
                <div>
                  <p style={{ fontSize: '12px', fontWeight: '600', color: '#0891b2', marginBottom: '10px' }}>
                    Inherited {agent.inheritedContext.entries.length} Relevant Context(s):
                  </p>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                    {agent.inheritedContext.entries.map((entry, index) => (
                      <div
                        key={entry.entry_id || index}
                        style={{
                          background: entry.entry_type === 'source'
                            ? 'linear-gradient(to right, #ecfeff, #eff6ff)'
                            : 'white',
                          padding: '12px',
                          borderRadius: '6px',
                          border: entry.entry_type === 'source' ? '1px solid #67e8f9' : '1px solid #a5f3fc',
                        }}
                      >
                        {/* Work Title (Key) */}
                        <div style={{ display: 'flex', alignItems: 'flex-start', gap: '8px', marginBottom: '8px' }}>
                          <span style={{ color: entry.entry_type === 'source' ? '#2563eb' : '#06b6d4', fontSize: '14px' }}>
                            {entry.entry_type === 'source' ? '📋' : '🔑'}
                          </span>
                          <div style={{ flex: 1 }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                              <span style={{ fontSize: '14px', fontWeight: '500', color: '#1f2937' }}>{entry.work_title}</span>
                              {entry.entry_type === 'source' && (
                                <span style={{ backgroundColor: '#dbeafe', color: '#1e40af', padding: '2px 6px', borderRadius: '4px', fontSize: '10px', fontWeight: '500' }}>Source Context</span>
                              )}
                            </div>
                          </div>
                        </div>

                        {/* Source Context Data (for source type entries) */}
                        {entry.entry_type === 'source' && entry.source_context && (
                          <div style={{ marginLeft: '22px' }}>
                            {/* Bug Summary */}
                            {entry.source_context.bug_summary && (
                              <div style={{ marginBottom: '6px' }}>
                                <div style={{ fontSize: '11px', fontWeight: '500', color: '#dc2626', marginBottom: '2px' }}>🐛 Bug/Issue:</div>
                                <div style={{ fontSize: '12px', color: '#374151', backgroundColor: '#fef2f2', padding: '8px', borderRadius: '4px' }}>{entry.source_context.bug_summary}</div>
                              </div>
                            )}
                            {/* Error Messages */}
                            {entry.source_context.error_messages && entry.source_context.error_messages.length > 0 && (
                              <div style={{ marginBottom: '6px' }}>
                                <div style={{ fontSize: '11px', fontWeight: '500', color: '#ea580c', marginBottom: '2px' }}>⚠️ Errors:</div>
                                <ul style={{ fontSize: '12px', color: '#374151', backgroundColor: '#fff7ed', padding: '8px', borderRadius: '4px', margin: 0, paddingLeft: '20px', fontFamily: 'monospace' }}>
                                  {entry.source_context.error_messages.map((err, idx) => (
                                    <li key={idx}>{err}</li>
                                  ))}
                                </ul>
                              </div>
                            )}
                            {/* File Paths */}
                            {entry.source_context.file_paths && entry.source_context.file_paths.length > 0 && (
                              <div style={{ marginBottom: '6px' }}>
                                <div style={{ fontSize: '11px', fontWeight: '500', color: '#16a34a', marginBottom: '2px' }}>📁 Files:</div>
                                <ul style={{ fontSize: '12px', color: '#374151', backgroundColor: '#f0fdf4', padding: '8px', borderRadius: '4px', margin: 0, paddingLeft: '20px', fontFamily: 'monospace' }}>
                                  {entry.source_context.file_paths.map((path, idx) => (
                                    <li key={idx}>{path}</li>
                                  ))}
                                </ul>
                              </div>
                            )}
                            {/* Commit References */}
                            {entry.source_context.commit_references && entry.source_context.commit_references.length > 0 && (
                              <div style={{ marginBottom: '6px' }}>
                                <div style={{ fontSize: '11px', fontWeight: '500', color: '#7c3aed', marginBottom: '2px' }}>🔖 Commits/Versions:</div>
                                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                                  {entry.source_context.commit_references.map((ref, idx) => (
                                    <span key={idx} style={{ backgroundColor: '#f3e8ff', color: '#7c3aed', padding: '2px 8px', borderRadius: '4px', fontSize: '11px', fontFamily: 'monospace' }}>{ref}</span>
                                  ))}
                                </div>
                              </div>
                            )}
                            {/* Key Facts */}
                            {entry.source_context.key_facts && entry.source_context.key_facts.length > 0 && (
                              <div style={{ marginBottom: '6px' }}>
                                <div style={{ fontSize: '11px', fontWeight: '500', color: '#ca8a04', marginBottom: '2px' }}>⭐ Key Facts:</div>
                                <ul style={{ fontSize: '12px', color: '#374151', backgroundColor: '#fefce8', padding: '8px', borderRadius: '4px', margin: 0, paddingLeft: '20px' }}>
                                  {entry.source_context.key_facts.map((fact, idx) => (
                                    <li key={idx}>{fact}</li>
                                  ))}
                                </ul>
                              </div>
                            )}
                            {/* CWE Information (inherited) */}
                            {entry.source_context.inferred_cwes && entry.source_context.inferred_cwes.length > 0 && (
                              <div style={{ marginBottom: '6px', backgroundColor: '#fef2f2', padding: '10px', borderRadius: '6px', border: '1px solid #fecaca' }}>
                                <div style={{ fontSize: '11px', fontWeight: '600', color: '#dc2626', marginBottom: '6px' }}>🛡️ CWE Pattern Analysis</div>
                                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginBottom: '6px' }}>
                                  {entry.source_context.inferred_cwes.map((cwe, idx) => (
                                    <span key={idx} style={{ backgroundColor: '#fee2e2', color: '#991b1b', padding: '3px 8px', borderRadius: '4px', fontSize: '11px', fontWeight: '600', fontFamily: 'monospace' }}>{cwe}</span>
                                  ))}
                                </div>
                                {entry.source_context.fix_patterns && Object.keys(entry.source_context.fix_patterns).length > 0 && (
                                  <div>
                                    <div style={{ fontSize: '10px', fontWeight: '500', color: '#16a34a', marginBottom: '2px' }}>🔧 Fix Patterns:</div>
                                    <div style={{ fontSize: '11px', color: '#374151', backgroundColor: 'white', padding: '6px', borderRadius: '4px' }}>
                                      {Object.entries(entry.source_context.fix_patterns).map(([cwe, pattern], idx) => (
                                        <div key={idx} style={{ marginBottom: idx < Object.keys(entry.source_context.fix_patterns).length - 1 ? '4px' : 0 }}>
                                          <span style={{ fontWeight: '500', color: '#15803d', fontFamily: 'monospace' }}>{cwe}:</span>
                                          <span style={{ marginLeft: '4px' }}>{pattern}</span>
                                        </div>
                                      ))}
                                    </div>
                                  </div>
                                )}
                              </div>
                            )}
                            {/* Tags */}
                            {entry.tags && entry.tags.length > 0 && (
                              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginTop: '6px' }}>
                                {entry.tags.map((tag, tagIdx) => (
                                  <span key={tagIdx} style={{ backgroundColor: '#dbeafe', color: '#1e40af', padding: '2px 8px', borderRadius: '9999px', fontSize: '11px' }}>{tag}</span>
                                ))}
                              </div>
                            )}
                          </div>
                        )}

                        {/* Worker Context Data (for worker type entries) */}
                        {entry.entry_type !== 'source' && (
                          <div style={{ marginLeft: '22px' }}>
                            {/* Objective */}
                            <div style={{ marginBottom: '6px' }}>
                              <span style={{ fontSize: '11px', fontWeight: '500', color: '#6b7280' }}>Objective: </span>
                              <span style={{ fontSize: '12px', color: '#374151' }}>{entry.objective}</span>
                            </div>
                            {/* Why This Is Relevant */}
                            {entry.justification && (
                              <div style={{ marginBottom: '6px' }}>
                                <span style={{ fontSize: '11px', fontWeight: '500', color: '#7c3aed' }}>Why This Is Relevant: </span>
                                <span style={{ fontSize: '12px', color: '#374151' }}>{entry.justification}</span>
                              </div>
                            )}
                            {/* Work Analysis - How it was accomplished */}
                            {entry.work_analysis && (
                              <div style={{ marginBottom: '6px' }}>
                                <div style={{ fontSize: '11px', fontWeight: '500', color: '#16a34a', marginBottom: '2px' }}>How It Was Accomplished:</div>
                                <div style={{ fontSize: '12px', color: '#374151', whiteSpace: 'pre-wrap', backgroundColor: '#f0fdf4', padding: '8px', borderRadius: '4px', maxHeight: '120px', overflow: 'auto' }}>
                                  {entry.work_analysis}
                                </div>
                              </div>
                            )}
                            {/* Tags */}
                            {entry.tags && entry.tags.length > 0 && (
                              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginTop: '6px' }}>
                                {entry.tags.map((tag, tagIdx) => (
                                  <span
                                    key={tagIdx}
                                    style={{
                                      backgroundColor: '#cffafe',
                                      color: '#0891b2',
                                      padding: '2px 8px',
                                      borderRadius: '9999px',
                                      fontSize: '11px',
                                    }}
                                  >
                                    {tag}
                                  </span>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <p style={{ fontSize: '12px', color: '#6b7280', fontStyle: 'italic' }}>
                  {agent.inheritedContext?.total_available > 0
                    ? `LLM evaluated ${agent.inheritedContext.total_available} available contexts but found none directly relevant to this specific task.`
                    : 'No context entries were available in the dashboard when this worker started.'}
                </p>
              )}
            </div>
          </Section>
        )}

        {/* Footer info */}
        <div style={{ marginTop: '16px', paddingTop: '16px', borderTop: '1px solid #e5e7eb', display: 'flex', gap: '16px', fontSize: '12px', color: '#9ca3af' }}>
          <span>Depth: Level {agent.depth}</span>
          <span>{agent.childrenCount} {agent.childrenCount === 1 ? 'child' : 'children'}</span>
        </div>
      </div>
    </div>
  );
}

const initialNodes = """
    component += nodes_json
    component += """;

const initialEdges = """
    component += edges_json
    component += """;

export default function ReactFlowTree() {
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const [selectedAgent, setSelectedAgent] = useState(null);

  const onConnect = useCallback(
    (params) => setEdges((eds) => addEdge(params, eds)),
    [setEdges]
  );

  const onNodeClick = useCallback((event, node) => {
    setSelectedAgent(node.data);
  }, []);

  const closeModal = useCallback(() => {
    setSelectedAgent(null);
  }, []);

  // MiniMap node color based on style background
  const minimapNodeColor = (node) => node.style?.background || '#6b7280';

  return (
    <div style={{ height: '100vh', width: '100%' }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeClick={onNodeClick}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.1}
        maxZoom={2}
      >
        <MiniMap nodeColor={minimapNodeColor} zoomable pannable />
        <Controls />
        <Background variant="dots" gap={12} size={1} />
      </ReactFlow>
      <AgentModal agent={selectedAgent} onClose={closeModal} />
    </div>
  );
}
"""
    return component


def print_agents_table(agents: list):
    """Print a formatted table of agents."""
    print("\nAvailable Agents:", file=sys.stderr)
    print("-" * 100, file=sys.stderr)
    print(
        f"{'#':<4} {'ID':<38} {'Role':<8} {'Status':<12} {'Task Description':<35}",
        file=sys.stderr,
    )
    print("-" * 100, file=sys.stderr)

    for i, agent in enumerate(agents):
        task = agent["task_description"]
        if len(task) > 35:
            task = task[:32] + "..."
        print(
            f"{i:<4} {agent['id']:<38} {agent['role']:<8} {agent['status']:<12} {task:<35}",
            file=sys.stderr,
        )

    print("-" * 100, file=sys.stderr)
    print(f"Total: {len(agents)} agents\n", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Generate ReactFlow component from agent hierarchy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/generate_reactflow.py              # Use latest agent
  python3 scripts/generate_reactflow.py --list       # List all available agents
  python3 scripts/generate_reactflow.py <agent-id>   # Use specific agent ID
  python3 scripts/generate_reactflow.py -n 2         # Use the 3rd most recent agent
        """,
    )
    parser.add_argument("agent_id", nargs="?", help="Specific agent ID to use")
    parser.add_argument(
        "--list", "-l", action="store_true", help="List all available agents"
    )
    parser.add_argument(
        "-n", type=int, default=0, help="Use nth agent from list (0=latest, default)"
    )

    args = parser.parse_args()

    # Fetch agents list
    print("Fetching agents list...", file=sys.stderr)
    agents = list_agents()

    if not agents:
        print("No agents found!", file=sys.stderr)
        sys.exit(1)

    # List mode - just print agents and exit
    if args.list:
        print_agents_table(agents)
        sys.exit(0)

    # Determine which agent ID to use
    if args.agent_id:
        agent_id = args.agent_id
        # Validate agent ID exists
        agent_ids = [a["id"] for a in agents]
        if agent_id not in agent_ids:
            print(f"Error: Agent ID '{agent_id}' not found!", file=sys.stderr)
            print("Use --list to see available agents.", file=sys.stderr)
            sys.exit(1)
    else:
        # Use nth agent (default 0 = latest)
        if args.n >= len(agents):
            print(
                f"Error: Index {args.n} out of range. Only {len(agents)} agents available.",
                file=sys.stderr,
            )
            sys.exit(1)
        agent_id = agents[args.n]["id"]
        print(f"Using agent #{args.n}: {agent_id}", file=sys.stderr)

    # Fetch hierarchy
    print(f"Fetching hierarchy for {agent_id}...", file=sys.stderr)
    hierarchy = get_hierarchy(agent_id)

    root = hierarchy["root"]
    total_agents = hierarchy.get("total_agents", "?")
    depth = hierarchy.get("depth", "?")
    print(f"Hierarchy: {total_agents} agents, {depth} levels deep", file=sys.stderr)

    # Collect all agent IDs and fetch their summaries
    print("Fetching agent summaries...", file=sys.stderr)
    agent_ids = collect_agent_ids(root)
    summaries = fetch_all_summaries(agent_ids)

    nodes = []
    edges = []
    layout_tree(root, 0, 0, nodes, edges, summaries)

    print(f"Generated {len(nodes)} nodes and {len(edges)} edges", file=sys.stderr)

    # Output the React component to stdout
    print(generate_react_component(nodes, edges))


if __name__ == "__main__":
    main()
