/**
 * Panel for displaying prompts sent by an agent.
 * Shows prompts for complexity evaluation, task decomposition, and worker execution.
 */

import { useState } from 'react';
import type { AgentPrompt } from '../types/api';

interface PromptsPanelProps {
  prompts: AgentPrompt[];
  total: number;
  loading: boolean;
  onRefresh: () => void;
}

const promptTypeLabels: Record<string, string> = {
  complexity_evaluation: 'Complexity Evaluation',
  task_decomposition: 'Task Decomposition',
  worker_execution: 'Worker Execution',
  researcher_execution: 'Research',
};

const promptTypeColors: Record<string, string> = {
  complexity_evaluation: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
  task_decomposition: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200',
  worker_execution: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
  researcher_execution: 'bg-cyan-100 text-cyan-800 dark:bg-cyan-900 dark:text-cyan-200',
};

export function PromptsPanel({
  prompts,
  total,
  loading,
  onRefresh,
}: PromptsPanelProps) {
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null);

  if (loading && prompts.length === 0) {
    return (
      <div className="p-4 text-gray-500">
        <div className="animate-pulse">Loading prompts...</div>
      </div>
    );
  }

  const toggleExpand = (index: number) => {
    setExpandedIndex(expandedIndex === index ? null : index);
  };

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-semibold text-gray-800 dark:text-white">
          Agent Prompts
        </h3>
        <div className="flex items-center gap-2">
          <span className="text-sm text-gray-500 dark:text-gray-400">
            {total} prompts
          </span>
          <button
            onClick={onRefresh}
            disabled={loading}
            className="px-2 py-1 text-xs bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
          >
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </div>

      {/* Description */}
      <p className="text-xs text-gray-500 dark:text-gray-400">
        Prompts sent to LLM or worker tools. Click to expand/collapse.
      </p>

      {/* Prompt List */}
      {prompts.length === 0 ? (
        <div className="text-gray-500 dark:text-gray-400 text-sm">
          No prompts captured yet. Prompts are captured when agents interact with LLM or worker tools.
        </div>
      ) : (
        <div className="space-y-3">
          {prompts.map((prompt, index) => (
            <div
              key={index}
              className="bg-gray-50 dark:bg-gray-800 rounded border border-gray-200 dark:border-gray-700"
            >
              {/* Prompt Header - clickable */}
              <button
                onClick={() => toggleExpand(index)}
                className="w-full p-3 text-left flex items-center justify-between hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors rounded-t"
              >
                <div className="flex items-center gap-3">
                  <span
                    className={`px-2 py-0.5 text-xs font-medium rounded ${
                      promptTypeColors[prompt.prompt_type] || 'bg-gray-100 text-gray-800'
                    }`}
                  >
                    {promptTypeLabels[prompt.prompt_type] || prompt.prompt_type}
                  </span>
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    → {prompt.target}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-400">
                    {new Date(prompt.occurred_at).toLocaleTimeString()}
                  </span>
                  <svg
                    className={`w-4 h-4 text-gray-400 transition-transform ${
                      expandedIndex === index ? 'rotate-180' : ''
                    }`}
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M19 9l-7 7-7-7"
                    />
                  </svg>
                </div>
              </button>

              {/* Prompt Content - collapsible */}
              {expandedIndex === index && (
                <div className="p-3 border-t border-gray-200 dark:border-gray-700">
                  <pre className="text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap font-mono bg-gray-100 dark:bg-gray-900 p-3 rounded max-h-96 overflow-auto">
                    {prompt.prompt}
                  </pre>
                  <div className="mt-2 flex justify-end">
                    <button
                      onClick={() => navigator.clipboard.writeText(prompt.prompt)}
                      className="text-xs text-blue-500 hover:text-blue-600"
                    >
                      Copy to clipboard
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
