/**
 * Panel for displaying agent summary information when a node is selected.
 * Shows task, complexity reasoning, config, and subtasks/tool info.
 */

import type { AgentSummary } from '../types/api';

interface SummaryPanelProps {
  summary: AgentSummary | null;
  loading: boolean;
}

const statusColors: Record<string, string> = {
  pending: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300',
  analyzing: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  in_progress: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  waiting: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
  completed: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
  blocked: 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300',
};

const roleIcons: Record<string, string> = {
  boss: '👑',
  manager: '📋',
  worker: '⚙️',
  pending: '⏳',
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-4">
      <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">
        {title}
      </h3>
      {children}
    </div>
  );
}

export function SummaryPanel({ summary, loading }: SummaryPanelProps) {
  if (loading) {
    return (
      <div className="p-4 text-gray-500">
        <div className="animate-pulse">Loading summary...</div>
      </div>
    );
  }

  if (!summary) {
    return (
      <div className="p-4 text-gray-500">
        Select an agent node to view its summary
      </div>
    );
  }

  const roleIcon = roleIcons[summary.role.toLowerCase()] || '❓';
  const statusClass = statusColors[summary.status.toLowerCase()] || statusColors.pending;

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full">
      {/* Header with role and status */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-2xl">{roleIcon}</span>
          <span className="font-semibold text-gray-800 dark:text-white capitalize">
            {summary.role}
          </span>
        </div>
        <span className={`px-2 py-1 text-xs rounded-full ${statusClass}`}>
          {summary.status}
        </span>
      </div>

      {/* Task Description (Objective) */}
      <Section title="Objective">
        <p className="text-sm text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-800 p-2 rounded">
          {summary.task_description || 'No task assigned'}
        </p>
      </Section>

      {/* Budget Information */}
      {summary.budget && (
        <Section title="Budget">
          <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded space-y-2">
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-500">Current Balance:</span>
              <span className="text-sm font-semibold text-green-600 dark:text-green-400">
                ${summary.budget.current_budget.toFixed(1)}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-500">Initial Allocation:</span>
              <span className="text-sm text-gray-700 dark:text-gray-300">
                ${summary.budget.initial_budget.toFixed(1)}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-xs text-gray-500">Spent:</span>
              <span className={`text-sm ${
                summary.budget.spent > 0
                  ? 'text-orange-600 dark:text-orange-400'
                  : 'text-gray-700 dark:text-gray-300'
              }`}>
                ${summary.budget.spent.toFixed(1)}
              </span>
            </div>
            {/* Progress bar */}
            <div className="mt-2">
              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
                <div
                  className="bg-green-500 h-2 rounded-full transition-all"
                  style={{
                    width: `${Math.min(100, (summary.budget.current_budget / summary.budget.initial_budget) * 100)}%`
                  }}
                />
              </div>
              <div className="flex justify-between text-xs text-gray-500 mt-1">
                <span>0</span>
                <span>{summary.budget.source || 'budget'}</span>
                <span>${summary.budget.initial_budget.toFixed(0)}</span>
              </div>
            </div>
          </div>
        </Section>
      )}

      {/* Complexity Evaluation (for non-BOSS agents) */}
      {summary.complexity && (
        <Section title="Complexity Evaluation">
          <div className="bg-gray-50 dark:bg-gray-800 p-2 rounded space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-gray-500">Result:</span>
              <span className={`px-2 py-0.5 text-xs rounded ${
                summary.complexity === 'simple'
                  ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
                  : 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300'
              }`}>
                {summary.complexity.toUpperCase()}
              </span>
              <span className="text-xs text-gray-500">
                → became {summary.complexity === 'simple' ? 'WORKER' : 'MANAGER'}
              </span>
            </div>
            {summary.complexity_reasoning && (
              <p className="text-xs text-gray-600 dark:text-gray-400 italic">
                "{summary.complexity_reasoning}"
              </p>
            )}
          </div>
        </Section>
      )}

      {/* Worker Tool (for WORKER agents) */}
      {summary.worker_tool && (
        <Section title="Worker Tool">
          <div className="flex items-center gap-2 bg-gray-50 dark:bg-gray-800 p-2 rounded">
            <span className="text-lg">
              {summary.worker_tool === 'claude_code' ? '🤖' : '🔧'}
            </span>
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
              {summary.worker_tool === 'claude_code' ? 'Claude Code' : 'OpenHands'}
            </span>
          </div>
        </Section>
      )}

      {/* Worker Report (for WORKER agents that have completed) */}
      {summary.worker_report && (
        <Section title="Worker Report">
          <div className="bg-green-50 dark:bg-green-900/20 p-3 rounded border border-green-200 dark:border-green-800 space-y-3">
            {summary.worker_report.approach && (
              <div>
                <span className="text-xs font-semibold text-green-700 dark:text-green-400">Approach:</span>
                <p className="text-sm text-gray-700 dark:text-gray-300 mt-0.5">
                  {summary.worker_report.approach}
                </p>
              </div>
            )}
            {summary.worker_report.reasoning && (
              <div>
                <span className="text-xs font-semibold text-green-700 dark:text-green-400">Reasoning:</span>
                <p className="text-sm text-gray-700 dark:text-gray-300 mt-0.5">
                  {summary.worker_report.reasoning}
                </p>
              </div>
            )}
            {summary.worker_report.deliverables && (
              <div>
                <span className="text-xs font-semibold text-green-700 dark:text-green-400">Deliverables:</span>
                <p className="text-sm text-gray-700 dark:text-gray-300 mt-0.5">
                  {summary.worker_report.deliverables}
                </p>
              </div>
            )}
            {summary.worker_report.challenges && summary.worker_report.challenges !== 'No significant challenges encountered' && (
              <div>
                <span className="text-xs font-semibold text-orange-700 dark:text-orange-400">Challenges:</span>
                <p className="text-sm text-gray-700 dark:text-gray-300 mt-0.5">
                  {summary.worker_report.challenges}
                </p>
              </div>
            )}
          </div>
        </Section>
      )}

      {/* Subtasks (for MANAGER agents) */}
      {summary.subtasks.length > 0 && (
        <Section title={`Subtasks (${summary.subtasks.length})`}>
          <div className="space-y-3">
            {summary.subtasks.map((subtask, idx) => (
              <div
                key={idx}
                className="bg-gray-50 dark:bg-gray-800 p-3 rounded border-l-2 border-blue-400"
              >
                {/* Description and status */}
                <div className="flex items-start justify-between gap-2">
                  <p className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-1">
                    {subtask.description}
                  </p>
                  {subtask.child_status && (
                    <span className={`px-1.5 py-0.5 text-xs rounded shrink-0 ${
                      statusColors[subtask.child_status] || statusColors.pending
                    }`}>
                      {subtask.child_status}
                    </span>
                  )}
                </div>

                {/* Budget weight */}
                {subtask.budget_weight && subtask.budget_weight !== 1.0 && (
                  <div className="mt-1 text-xs text-gray-500">
                    <span className="font-medium">Budget weight:</span> {subtask.budget_weight.toFixed(1)}x
                  </div>
                )}

                {/* Justification (collapsible) */}
                {subtask.justification && subtask.justification.objective && (
                  <details className="mt-2">
                    <summary className="text-xs text-blue-600 dark:text-blue-400 cursor-pointer hover:underline">
                      View supervisor's justification
                    </summary>
                    <div className="mt-2 space-y-2 text-xs bg-blue-50 dark:bg-blue-900/20 p-2 rounded">
                      {subtask.justification.parent_task && subtask.justification.parent_task !== '(legacy event)' && (
                        <div>
                          <span className="font-semibold text-gray-600 dark:text-gray-400">Parent Task:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{subtask.justification.parent_task}</p>
                        </div>
                      )}
                      {subtask.justification.split_reason && subtask.justification.split_reason !== '(legacy event)' && (
                        <div>
                          <span className="font-semibold text-gray-600 dark:text-gray-400">Why Split:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{subtask.justification.split_reason}</p>
                        </div>
                      )}
                      {subtask.justification.objective && subtask.justification.objective !== '(legacy event)' && (
                        <div>
                          <span className="font-semibold text-gray-600 dark:text-gray-400">Objective:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{subtask.justification.objective}</p>
                        </div>
                      )}
                      {subtask.justification.plan && subtask.justification.plan !== '(legacy event)' && (
                        <div>
                          <span className="font-semibold text-gray-600 dark:text-gray-400">Plan:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{subtask.justification.plan}</p>
                        </div>
                      )}
                      {subtask.justification.why_it_may_work && subtask.justification.why_it_may_work !== '(legacy event)' && (
                        <div>
                          <span className="font-semibold text-gray-600 dark:text-gray-400">Why It May Work:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{subtask.justification.why_it_may_work}</p>
                        </div>
                      )}
                      {subtask.justification.expected_results && subtask.justification.expected_results !== '(legacy event)' && (
                        <div>
                          <span className="font-semibold text-gray-600 dark:text-gray-400">Expected Results:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{subtask.justification.expected_results}</p>
                        </div>
                      )}
                      {/* Budget Allocation Reasoning */}
                      {subtask.justification.budget_allocation && (
                        <div className="mt-2 pt-2 border-t border-purple-200 dark:border-purple-700">
                          <span className="font-semibold text-purple-600 dark:text-purple-400">Budget Allocation:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{subtask.justification.budget_allocation}</p>
                        </div>
                      )}
                      {subtask.justification.complexity_assessment && (
                        <div>
                          <span className="font-semibold text-purple-600 dark:text-purple-400">Complexity:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{subtask.justification.complexity_assessment}</p>
                        </div>
                      )}
                      {subtask.justification.significance_weight && (
                        <div>
                          <span className="font-semibold text-purple-600 dark:text-purple-400">Significance:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{subtask.justification.significance_weight}</p>
                        </div>
                      )}
                      {subtask.justification.resource_justification && (
                        <div>
                          <span className="font-semibold text-purple-600 dark:text-purple-400">Resource Justification:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{subtask.justification.resource_justification}</p>
                        </div>
                      )}
                    </div>
                  </details>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Accumulated Worker Reports (for MANAGER/BOSS agents) */}
      {summary.child_worker_reports && summary.child_worker_reports.length > 0 && (
        <Section title={`Worker Reports (${summary.child_worker_reports.length})`}>
          <div className="space-y-3">
            {summary.child_worker_reports.map((childReport, idx) => (
              <div
                key={idx}
                className="bg-blue-50 dark:bg-blue-900/20 p-3 rounded border-l-2 border-blue-400"
              >
                {/* Header with agent ID and status */}
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-mono bg-blue-100 dark:bg-blue-800 px-1.5 py-0.5 rounded text-blue-700 dark:text-blue-300">
                      {childReport.agent_id}
                    </span>
                    <span className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[200px]">
                      {childReport.task}
                    </span>
                  </div>
                  <span className={`px-1.5 py-0.5 text-xs rounded ${
                    statusColors[childReport.status] || statusColors.pending
                  }`}>
                    {childReport.status}
                  </span>
                </div>

                {/* Worker Report Details */}
                {childReport.report && (
                  <details className="mt-2">
                    <summary className="text-xs text-blue-600 dark:text-blue-400 cursor-pointer hover:underline">
                      View work report
                    </summary>
                    <div className="mt-2 space-y-2 text-xs bg-white dark:bg-gray-800 p-2 rounded">
                      {childReport.report.approach && (
                        <div>
                          <span className="font-semibold text-green-700 dark:text-green-400">Approach:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{childReport.report.approach}</p>
                        </div>
                      )}
                      {childReport.report.reasoning && (
                        <div>
                          <span className="font-semibold text-green-700 dark:text-green-400">Reasoning:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{childReport.report.reasoning}</p>
                        </div>
                      )}
                      {childReport.report.deliverables && (
                        <div>
                          <span className="font-semibold text-green-700 dark:text-green-400">Deliverables:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{childReport.report.deliverables}</p>
                        </div>
                      )}
                      {childReport.report.challenges && childReport.report.challenges !== 'No significant challenges encountered' && (
                        <div>
                          <span className="font-semibold text-orange-700 dark:text-orange-400">Challenges:</span>
                          <p className="text-gray-700 dark:text-gray-300 mt-0.5">{childReport.report.challenges}</p>
                        </div>
                      )}
                    </div>
                  </details>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Aggregated Summary (for MANAGER/BOSS agents) */}
      {summary.aggregated_summary && (
        <Section title="Work Summary">
          <div className="bg-purple-50 dark:bg-purple-900/20 p-3 rounded border border-purple-200 dark:border-purple-800 space-y-3">
            {/* Worker Statistics */}
            <div className="flex items-center gap-4 text-sm flex-wrap">
              <div className="flex items-center gap-1">
                <span className="text-purple-600 dark:text-purple-400 font-semibold">
                  {summary.aggregated_summary.total_workers}
                </span>
                <span className="text-gray-500 dark:text-gray-400">workers</span>
              </div>
              <div className="flex items-center gap-1">
                <span className="text-green-600 dark:text-green-400 font-semibold">
                  {summary.aggregated_summary.completed_workers}
                </span>
                <span className="text-gray-500 dark:text-gray-400">completed</span>
              </div>
              {summary.aggregated_summary.failed_workers > 0 && (
                <div className="flex items-center gap-1">
                  <span className="text-red-600 dark:text-red-400 font-semibold">
                    {summary.aggregated_summary.failed_workers}
                  </span>
                  <span className="text-gray-500 dark:text-gray-400">failed</span>
                </div>
              )}
            </div>

            {/* Combined Deliverables (includes tools used header) */}
            {summary.aggregated_summary.combined_deliverables && (
              <div>
                <span className="text-xs font-semibold text-purple-700 dark:text-purple-400">Deliverables & Results:</span>
                <div className="text-xs text-gray-700 dark:text-gray-300 mt-1 whitespace-pre-wrap bg-white dark:bg-gray-800 p-2 rounded max-h-64 overflow-y-auto font-mono leading-relaxed">
                  {summary.aggregated_summary.combined_deliverables}
                </div>
              </div>
            )}

            {/* Combined Approach */}
            {summary.aggregated_summary.combined_approach && (
              <details className="mt-2">
                <summary className="text-xs text-purple-600 dark:text-purple-400 cursor-pointer hover:underline font-semibold">
                  Approaches & Reasoning
                </summary>
                <div className="text-xs text-gray-700 dark:text-gray-300 mt-1 whitespace-pre-wrap bg-white dark:bg-gray-800 p-2 rounded max-h-56 overflow-y-auto font-mono leading-relaxed">
                  {summary.aggregated_summary.combined_approach}
                </div>
              </details>
            )}

            {/* Key Challenges */}
            {summary.aggregated_summary.key_challenges && (
              <details className="mt-2">
                <summary className="text-xs text-orange-600 dark:text-orange-400 cursor-pointer hover:underline font-semibold">
                  Challenges Encountered
                </summary>
                <div className="text-xs text-gray-700 dark:text-gray-300 mt-1 whitespace-pre-wrap bg-white dark:bg-gray-800 p-2 rounded max-h-48 overflow-y-auto font-mono leading-relaxed">
                  {summary.aggregated_summary.key_challenges}
                </div>
              </details>
            )}
          </div>
        </Section>
      )}

      {/* Task Queue (pending tasks) */}
      {summary.queue_size > 0 && (
        <Section title={`Task Queue (${summary.queue_size})`}>
          <div className="space-y-2">
            {summary.task_queue.map((task, idx) => (
              <div
                key={idx}
                className="bg-yellow-50 dark:bg-yellow-900/20 p-2 rounded border-l-2 border-yellow-400 flex items-center gap-2"
              >
                <span className="text-yellow-600 dark:text-yellow-400">
                  {idx + 1}.
                </span>
                <p className="text-sm text-gray-700 dark:text-gray-300 flex-1">
                  {task.description}
                </p>
                {task.priority > 0 && (
                  <span className="px-1.5 py-0.5 text-xs bg-orange-100 dark:bg-orange-900/30 text-orange-800 dark:text-orange-300 rounded">
                    P{task.priority}
                  </span>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Configuration */}
      {summary.config_strategy && (
        <Section title="Configuration">
          <div className="bg-gray-50 dark:bg-gray-800 p-2 rounded space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-gray-500">Strategy:</span>
              <span className="text-xs text-gray-700 dark:text-gray-300">
                {summary.config_strategy}
              </span>
            </div>
            {Object.keys(summary.config_details).length > 0 && (
              <pre className="text-xs text-gray-600 dark:text-gray-400 overflow-x-auto">
                {JSON.stringify(summary.config_details, null, 2)}
              </pre>
            )}
          </div>
        </Section>
      )}

      {/* Result (if completed) */}
      {summary.result && (
        <Section title="Result">
          <div className="bg-green-50 dark:bg-green-900/20 p-2 rounded border-l-2 border-green-500">
            <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
              {summary.result}
            </p>
          </div>
        </Section>
      )}

      {/* Error (if failed) */}
      {summary.error_message && (
        <Section title="Error">
          <div className="bg-red-50 dark:bg-red-900/20 p-2 rounded border-l-2 border-red-500">
            <p className="text-sm text-red-700 dark:text-red-300">
              {summary.error_message}
            </p>
          </div>
        </Section>
      )}

      {/* Published Context (for BOSS/MANAGER - context they contributed to dashboard) */}
      {summary.published_context && summary.published_context.length > 0 && (
        <Section title="📤 Context Published to Dashboard">
          <div className="space-y-4">
            <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
              This supervisor published {summary.published_context.length} context{summary.published_context.length !== 1 ? 's' : ''} to the global knowledge dashboard for cross-session learning.
            </p>
            {summary.published_context.map((ctx) => (
              <div
                key={ctx.entry_id}
                className="bg-purple-50 dark:bg-purple-900/20 p-3 rounded border-l-2 border-purple-400"
              >
                {/* Key (work_title) */}
                <div className="flex items-start gap-2 mb-2">
                  <span className="text-purple-500 text-sm">🔑</span>
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-purple-600 dark:text-purple-400 font-semibold uppercase">Key</p>
                    <p className="text-sm font-medium text-gray-800 dark:text-gray-200">
                      {ctx.work_title}
                    </p>
                  </div>
                </div>

                {/* Value Section */}
                <div className="ml-6 space-y-2 border-t border-purple-200 dark:border-purple-700 pt-2">
                  <p className="text-xs text-purple-600 dark:text-purple-400 font-semibold uppercase">Value</p>

                  {/* Objective */}
                  <div>
                    <p className="text-xs font-medium text-gray-600 dark:text-gray-400">Objective:</p>
                    <p className="text-xs text-gray-700 dark:text-gray-300">{ctx.objective}</p>
                  </div>

                  {/* Justification */}
                  {ctx.justification && (
                    <div>
                      <p className="text-xs font-medium text-gray-600 dark:text-gray-400">Why Assigned:</p>
                      <p className="text-xs text-gray-700 dark:text-gray-300">{ctx.justification}</p>
                    </div>
                  )}

                  {/* Work Analysis (comprehensive) */}
                  {ctx.work_analysis && (
                    <div>
                      <p className="text-xs font-medium text-gray-600 dark:text-gray-400">How It Was Accomplished:</p>
                      <div className="text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap bg-white/50 dark:bg-gray-800/50 p-2 rounded max-h-48 overflow-y-auto">
                        {ctx.work_analysis}
                      </div>
                    </div>
                  )}

                  {/* Approach */}
                  {ctx.approach && (
                    <div>
                      <p className="text-xs font-medium text-gray-600 dark:text-gray-400">Approach:</p>
                      <p className="text-xs text-gray-700 dark:text-gray-300">{ctx.approach}</p>
                    </div>
                  )}

                  {/* Challenges */}
                  {ctx.challenges && (
                    <div>
                      <p className="text-xs font-medium text-gray-600 dark:text-gray-400">Challenges:</p>
                      <p className="text-xs text-gray-700 dark:text-gray-300">{ctx.challenges}</p>
                    </div>
                  )}

                  {/* Tags */}
                  {ctx.tags && ctx.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {ctx.tags.map((tag, tagIdx) => (
                        <span
                          key={tagIdx}
                          className="px-1.5 py-0.5 text-xs bg-purple-100 dark:bg-purple-800 text-purple-700 dark:text-purple-300 rounded"
                        >
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}

                  {/* Metadata */}
                  <div className="flex items-center gap-2 pt-1 border-t border-purple-100 dark:border-purple-800">
                    <span className="text-xs text-gray-400">
                      Worker: {ctx.worker_id.substring(0, 8)}...
                    </span>
                    <span className="text-xs text-gray-400">
                      {new Date(ctx.published_at).toLocaleString()}
                    </span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Inherited Context (for WORKER - context they received from dashboard) */}
      {summary.inherited_context && summary.role.toLowerCase() === 'worker' && (
        <Section title="📥 Inherited Knowledge from Dashboard">
          <div className="bg-cyan-50 dark:bg-cyan-900/20 p-3 rounded border-l-2 border-cyan-400">
            <div className="flex items-center gap-2 mb-2">
              <span className="text-cyan-500 text-lg">🧠</span>
              <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
                Cross-Session Learning Active
              </p>
            </div>
            <p className="text-xs text-gray-600 dark:text-gray-400 mb-3">
              This worker inherited knowledge from {summary.inherited_context.total_available} available context entries in the global dashboard.
              Relevant knowledge was automatically identified and provided to help with this task.
            </p>

            {/* Reminder Banner */}
            <div className="bg-amber-50 dark:bg-amber-900/20 p-2 rounded mb-3 border border-amber-200 dark:border-amber-700">
              <p className="text-xs text-amber-700 dark:text-amber-300 font-medium">
                ⚠️ Learning Reminders:
              </p>
              <ul className="text-xs text-amber-600 dark:text-amber-400 mt-1 space-y-0.5 pl-3">
                <li>• Do NOT repeat work that has already been completed</li>
                <li>• Avoid repeating the same mistakes encountered before</li>
                <li>• Build upon successful approaches from previous work</li>
              </ul>
            </div>

            {summary.inherited_context.entries.length > 0 ? (
              <div className="space-y-3">
                <p className="text-xs font-semibold text-cyan-600 dark:text-cyan-400">
                  Inherited {summary.inherited_context.entries.length} Relevant Context(s):
                </p>
                {summary.inherited_context.entries.map((entry) => (
                  <div key={entry.entry_id} className="bg-white dark:bg-gray-800 p-3 rounded border border-cyan-200 dark:border-cyan-700">
                    {/* Work Title (Key) */}
                    <div className="flex items-start gap-2 mb-2">
                      <span className="text-cyan-500 text-sm">🔑</span>
                      <p className="text-sm font-medium text-gray-800 dark:text-gray-200">
                        {entry.work_title}
                      </p>
                    </div>

                    {/* Objective */}
                    <div className="ml-6 space-y-2">
                      <div>
                        <p className="text-xs font-medium text-gray-500 dark:text-gray-400">Objective:</p>
                        <p className="text-xs text-gray-700 dark:text-gray-300">{entry.objective}</p>
                      </div>

                      {/* Work Analysis - How it was accomplished */}
                      {entry.work_analysis && (
                        <div>
                          <p className="text-xs font-medium text-green-600 dark:text-green-400">How It Was Accomplished:</p>
                          <div className="text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap bg-green-50 dark:bg-green-900/20 p-2 rounded max-h-40 overflow-y-auto">
                            {entry.work_analysis}
                          </div>
                        </div>
                      )}

                      {/* Approach */}
                      {entry.approach && (
                        <div>
                          <p className="text-xs font-medium text-blue-600 dark:text-blue-400">Approach Used:</p>
                          <p className="text-xs text-gray-700 dark:text-gray-300 bg-blue-50 dark:bg-blue-900/20 p-2 rounded">
                            {entry.approach}
                          </p>
                        </div>
                      )}

                      {/* How This Can Benefit Current Work */}
                      {entry.justification && (
                        <div>
                          <p className="text-xs font-medium text-purple-600 dark:text-purple-400">Why This Is Relevant:</p>
                          <p className="text-xs text-gray-700 dark:text-gray-300">{entry.justification}</p>
                        </div>
                      )}

                      {/* Challenges - What to avoid */}
                      {entry.challenges && (
                        <div className="bg-red-50 dark:bg-red-900/20 p-2 rounded border border-red-200 dark:border-red-700">
                          <p className="text-xs font-medium text-red-600 dark:text-red-400">⚠️ Challenges to Avoid:</p>
                          <p className="text-xs text-gray-700 dark:text-gray-300">{entry.challenges}</p>
                        </div>
                      )}

                      {/* Tags */}
                      {entry.tags && entry.tags.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-1">
                          {entry.tags.map((tag, tagIdx) => (
                            <span
                              key={tagIdx}
                              className="px-1.5 py-0.5 text-xs bg-cyan-100 dark:bg-cyan-800 text-cyan-700 dark:text-cyan-300 rounded"
                            >
                              {tag}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs text-gray-500 dark:text-gray-500 italic">
                LLM evaluated {summary.inherited_context.total_available} available contexts but found none directly relevant to this specific task.
              </p>
            )}
          </div>
        </Section>
      )}
    </div>
  );
}
