/**
 * Panel for displaying agent summary information when a node is selected.
 * Shows task, complexity reasoning, config, and subtasks/tool info.
 * For workers, shows CLI-like output stream.
 */

import { useRef, useEffect, useState } from 'react';
import type { AgentSummary, BriefingSummary, DomainEvent, ThoughtCapturedData } from '../types/api';

interface SummaryPanelProps {
  summary: AgentSummary | null;
  loading: boolean;
  /** ThoughtCaptured events for worker output display */
  workerOutput?: DomainEvent[];
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

/** Extract content from ThoughtCaptured event */
function getThoughtContent(event: DomainEvent): string | null {
  if (event.event_type !== 'ThoughtCaptured') return null;
  const data = event.data as Partial<ThoughtCapturedData>;
  return data.content || null;
}

const justificationLabels: Record<string, string> = {
  objective: 'Objective',
  plan: 'Suggested Approach',
  security_insights: 'Security Insights',
};

/** Briefing context from parent agent */
function BriefingContextSection({ briefing }: { briefing: BriefingSummary }) {
  const [expanded, setExpanded] = useState(false);
  const hasJustification = Object.keys(briefing.subtask_justification).length > 0;
  const hasAncestry = briefing.ancestry.length > 0;
  const hasDecisions = briefing.decisions.length > 0;

  return (
    <Section title="Supervisor Context">
      <div className="bg-indigo-50 dark:bg-indigo-900/20 p-3 rounded border-l-2 border-indigo-400 space-y-2">
        {/* Parent info */}
        <div className="flex items-center gap-2 text-xs">
          <span className="font-medium text-indigo-600 dark:text-indigo-300">
            Assigned by:
          </span>
          <span className="px-1.5 py-0.5 rounded bg-indigo-100 dark:bg-indigo-800/40 text-indigo-700 dark:text-indigo-300 uppercase text-[10px] font-semibold">
            {briefing.parent_role}
          </span>
        </div>
        <p className="text-xs text-gray-600 dark:text-gray-400 italic">
          "{briefing.parent_task}"
        </p>

        {/* Justification fields */}
        {hasJustification && (
          <div className="pt-1 space-y-1.5">
            {Object.entries(briefing.subtask_justification).map(([key, value]) => (
              <div key={key}>
                <span className="text-[10px] font-semibold text-indigo-500 dark:text-indigo-400 uppercase tracking-wider">
                  {justificationLabels[key] || key.replace(/_/g, ' ')}
                </span>
                <p className="text-xs text-gray-700 dark:text-gray-300 mt-0.5">
                  {value}
                </p>
              </div>
            ))}
          </div>
        )}

        {/* Expandable ancestry & decisions */}
        {(hasAncestry || hasDecisions) && (
          <div className="pt-1">
            <button
              onClick={() => setExpanded(!expanded)}
              className="text-[10px] text-indigo-500 dark:text-indigo-400 hover:text-indigo-700 dark:hover:text-indigo-200 transition-colors"
            >
              {expanded ? '▾ Hide lineage' : '▸ Show lineage'}
              {hasAncestry && ` (${briefing.ancestry.length} ancestors)`}
            </button>
            {expanded && (
              <div className="mt-2 space-y-2">
                {hasAncestry && (
                  <div>
                    <span className="text-[10px] font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                      Ancestry
                    </span>
                    <div className="mt-1 space-y-1">
                      {briefing.ancestry.map((ancestor, idx) => (
                        <div key={idx} className="flex items-start gap-1.5 text-xs">
                          <span className="text-gray-400 dark:text-gray-500 shrink-0">
                            {'  '.repeat(idx)}→
                          </span>
                          <span className="px-1 py-0.5 rounded bg-gray-200 dark:bg-gray-700 text-[10px] font-semibold uppercase shrink-0">
                            {ancestor.role}
                          </span>
                          <span className="text-gray-600 dark:text-gray-400 truncate">
                            {ancestor.task_summary}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {hasDecisions && (
                  <div>
                    <span className="text-[10px] font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                      Inherited Decisions
                    </span>
                    <ul className="mt-1 space-y-0.5">
                      {briefing.decisions.map((decision, idx) => (
                        <li key={idx} className="text-xs text-gray-600 dark:text-gray-400 flex items-start gap-1">
                          <span className="text-indigo-400 shrink-0">•</span>
                          {decision}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </Section>
  );
}

/** CLI-like output display for worker agents */
function WorkerOutputSection({ events }: { events: DomainEvent[] }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  // Auto-scroll to bottom when new content arrives
  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events, autoScroll]);

  // Extract text content from ThoughtCaptured events
  const outputLines = events
    .map(getThoughtContent)
    .filter((content): content is string => content !== null);

  if (outputLines.length === 0) {
    return (
      <Section title="Worker Output">
        <div className="bg-gray-900 text-gray-400 p-3 rounded font-mono text-xs h-48 flex items-center justify-center">
          Waiting for output...
        </div>
      </Section>
    );
  }

  return (
    <Section title="Worker Output">
      <div className="relative">
        {/* Auto-scroll toggle */}
        <button
          onClick={() => setAutoScroll(!autoScroll)}
          className={`absolute top-2 right-2 z-10 px-2 py-0.5 text-xs rounded transition-colors ${
            autoScroll
              ? 'bg-green-600 text-white'
              : 'bg-gray-600 text-gray-300'
          }`}
          title={autoScroll ? 'Auto-scroll enabled' : 'Auto-scroll disabled'}
        >
          {autoScroll ? '⬇ Live' : '⏸ Paused'}
        </button>

        {/* CLI-style output */}
        <div
          ref={scrollRef}
          className="bg-gray-900 text-green-400 p-3 rounded font-mono text-xs h-64 overflow-y-auto whitespace-pre-wrap"
        >
          {outputLines.join('\n')}
        </div>
      </div>
    </Section>
  );
}

export function SummaryPanel({ summary, loading, workerOutput = [] }: SummaryPanelProps) {
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
  const isWorker = summary.role.toLowerCase() === 'worker';

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

      {/* Task Description */}
      <Section title="Task">
        <p className="text-sm text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-800 p-2 rounded">
          {summary.task_description || 'No task assigned'}
        </p>
      </Section>

      {/* Supervisor Context (briefing from parent — not shown for BOSS) */}
      {summary.briefing && <BriefingContextSection briefing={summary.briefing} />}

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

      {/* Worker Output (CLI-like display for WORKER agents) */}
      {isWorker && <WorkerOutputSection events={workerOutput} />}

      {/* Subtasks (for MANAGER agents) */}
      {summary.subtasks.length > 0 && (
        <Section title={`Subtasks (${summary.subtasks.length})`}>
          <div className="space-y-2">
            {summary.subtasks.map((subtask, idx) => {
              const hasJustification = Object.keys(subtask.justification).length > 0;
              return (
                <div
                  key={idx}
                  className="bg-gray-50 dark:bg-gray-800 p-2 rounded border-l-2 border-blue-400"
                >
                  <p className="text-sm text-gray-700 dark:text-gray-300">
                    {subtask.description}
                  </p>
                  {subtask.child_status && (
                    <div className="mt-1 flex items-center gap-2">
                      <span className={`px-1.5 py-0.5 text-xs rounded ${
                        statusColors[subtask.child_status] || statusColors.pending
                      }`}>
                        {subtask.child_status}
                      </span>
                    </div>
                  )}
                  {hasJustification && (
                    <div className="mt-2 pt-2 border-t border-gray-200 dark:border-gray-700 space-y-1">
                      {Object.entries(subtask.justification).map(([key, value]) => (
                        <div key={key} className="text-xs">
                          <span className="font-medium text-blue-500 dark:text-blue-400">
                            {justificationLabels[key] || key.replace(/_/g, ' ')}:
                          </span>{' '}
                          <span className="text-gray-600 dark:text-gray-400">{value}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
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
    </div>
  );
}
