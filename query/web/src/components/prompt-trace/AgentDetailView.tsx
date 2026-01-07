/**
 * Single-agent detail view component.
 *
 * Displays comprehensive information about an agent including:
 * - Header with role, depth, task, agent_id
 * - List of ParsedPromptCard components for each prompt
 * - Back button to return to tree view
 */

import type { TraceAgentNode } from '../../types/api';
import { ParsedPromptCard } from './ParsedPromptCard';

interface AgentDetailViewProps {
  /** The agent node to display. */
  agent: TraceAgentNode;
  /** Callback when back button is clicked. */
  onBack: () => void;
}

const roleColors: Record<string, string> = {
  boss: 'bg-purple-600',
  BOSS: 'bg-purple-600',
  manager: 'bg-blue-600',
  MANAGER: 'bg-blue-600',
  worker: 'bg-green-600',
  WORKER: 'bg-green-600',
  pending: 'bg-yellow-500',
  PENDING: 'bg-yellow-500',
};

export function AgentDetailView({ agent, onBack }: AgentDetailViewProps) {
  const roleLabel = agent.role.toUpperCase();
  const roleColor = roleColors[agent.role] ?? 'bg-gray-500';

  return (
    <div className="h-full overflow-y-auto">
      {/* Header */}
      <div className="sticky top-0 bg-white border-b border-gray-200 px-6 py-4 z-10">
        <div className="flex items-center gap-4">
          {/* Back button */}
          <button
            onClick={onBack}
            className="flex items-center gap-1 text-gray-600 hover:text-gray-900 transition-colors"
          >
            <svg
              className="w-5 h-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M15 19l-7-7 7-7"
              />
            </svg>
            <span>Back to Tree</span>
          </button>

          <div className="h-6 w-px bg-gray-300" />

          {/* Agent info */}
          <div className="flex items-center gap-3">
            <span className={`px-3 py-1 rounded text-white font-bold text-sm ${roleColor}`}>
              {roleLabel}
            </span>
            <span className="text-gray-500 text-sm">
              Depth: {agent.depth}
            </span>
            <span className="text-gray-500 text-sm">
              Sibling #{agent.sibling_index + 1}
            </span>
          </div>
        </div>

        {/* Task description */}
        <div className="mt-3">
          <p className="text-gray-700 text-sm">{agent.task}</p>
        </div>

        {/* Agent ID */}
        <div className="mt-2">
          <code className="text-xs text-gray-400 font-mono">{agent.agent_id}</code>
        </div>
      </div>

      {/* Prompts list */}
      <div className="p-6 space-y-6">
        {agent.prompts.length === 0 ? (
          <div className="text-center py-12 text-gray-500">
            No prompts recorded for this agent.
          </div>
        ) : (
          <>
            <div className="text-sm text-gray-500 mb-4">
              {agent.prompts.length} prompt{agent.prompts.length !== 1 ? 's' : ''} sent
            </div>
            {agent.prompts.map((prompt, idx) => (
              <ParsedPromptCard
                key={`${prompt.occurred_at}-${idx}`}
                prompt={prompt}
                promptNumber={idx + 1}
              />
            ))}
          </>
        )}
      </div>
    </div>
  );
}
