/**
 * Panel for displaying agent summary information when a node is selected.
 * Shows task, complexity reasoning, config, and subtasks/tool info.
 * For workers, shows CLI-like output stream.
 */

import { useRef, useEffect, useState } from 'react';
import type { AgentSummary, DomainEvent, ThoughtCapturedData, ResearchToolCall } from '../types/api';

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
  researcher: '🔍',
  pending: '⏳',
};

const toolIcons: Record<string, string> = {
  file_read: '📄',
  grep_search: '🔎',
  list_files: '📁',
  web_fetch: '🌐',
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

/** Research findings and tool calls display */
function ResearchSection({ findings, toolCalls }: { findings: string | null; toolCalls: ResearchToolCall[] }) {
  const [expandedCalls, setExpandedCalls] = useState<Set<number>>(new Set());

  const toggleExpand = (idx: number) => {
    setExpandedCalls(prev => {
      const next = new Set(prev);
      if (next.has(idx)) {
        next.delete(idx);
      } else {
        next.add(idx);
      }
      return next;
    });
  };

  return (
    <Section title="Research">
      {/* Findings Summary */}
      {findings && (
        <div className="bg-blue-50 dark:bg-blue-900/20 p-2 rounded border-l-2 border-blue-500 mb-3">
          <p className="text-xs font-medium text-gray-500 mb-1">Findings</p>
          <p className="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
            {findings}
          </p>
        </div>
      )}

      {/* Tool Calls */}
      {toolCalls.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs font-medium text-gray-500">Tool Calls ({toolCalls.length})</p>
          {toolCalls.map((call, idx) => {
            const isExpanded = expandedCalls.has(idx);
            const icon = toolIcons[call.tool_name] || '🔧';

            return (
              <div
                key={idx}
                className="bg-gray-50 dark:bg-gray-800 rounded border border-gray-200 dark:border-gray-700 overflow-hidden"
              >
                {/* Tool call header */}
                <button
                  onClick={() => toggleExpand(idx)}
                  className="w-full px-2 py-1.5 flex items-center gap-2 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                >
                  <span>{icon}</span>
                  <span className="text-xs font-medium text-gray-700 dark:text-gray-300">
                    {call.tool_name}
                  </span>
                  <span className="text-xs text-gray-500 truncate flex-1 text-left">
                    {JSON.stringify(call.arguments).slice(0, 50)}...
                  </span>
                  <span className="text-xs text-gray-400">
                    {isExpanded ? '▼' : '▶'}
                  </span>
                </button>

                {/* Expanded details */}
                {isExpanded && (
                  <div className="border-t border-gray-200 dark:border-gray-700 p-2 space-y-2">
                    <div>
                      <p className="text-xs font-medium text-gray-500 mb-1">Arguments</p>
                      <pre className="text-xs text-gray-600 dark:text-gray-400 bg-gray-100 dark:bg-gray-900 p-2 rounded overflow-x-auto">
                        {JSON.stringify(call.arguments, null, 2)}
                      </pre>
                    </div>
                    {call.result && (
                      <div>
                        <p className="text-xs font-medium text-gray-500 mb-1">Result</p>
                        <pre className="text-xs text-gray-600 dark:text-gray-400 bg-gray-100 dark:bg-gray-900 p-2 rounded overflow-x-auto max-h-48 overflow-y-auto">
                          {call.result.length > 2000 ? call.result.slice(0, 2000) + '...' : call.result}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
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

      {/* Complexity Evaluation (for non-BOSS agents) */}
      {summary.complexity && (
        <Section title="Complexity Evaluation">
          <div className="bg-gray-50 dark:bg-gray-800 p-2 rounded space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs font-medium text-gray-500">Result:</span>
              <span className={`px-2 py-0.5 text-xs rounded ${
                summary.complexity === 'simple'
                  ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
                  : 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300'
              }`}>
                {summary.complexity.toUpperCase()}
              </span>
              <span className="text-xs text-gray-500">
                → became {summary.complexity === 'simple' ? 'WORKER' : (summary.research_findings ? 'RESEARCHER → MANAGER' : 'MANAGER')}
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

      {/* Research (for agents that went through RESEARCHER phase) */}
      {(summary.research_findings || summary.research_tool_calls.length > 0) && (
        <ResearchSection
          findings={summary.research_findings}
          toolCalls={summary.research_tool_calls}
        />
      )}

      {/* Worker Output (CLI-like display for WORKER agents) */}
      {isWorker && <WorkerOutputSection events={workerOutput} />}

      {/* Subtasks (for MANAGER agents) */}
      {summary.subtasks.length > 0 && (
        <Section title={`Subtasks (${summary.subtasks.length})`}>
          <div className="space-y-2">
            {summary.subtasks.map((subtask, idx) => (
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
    </div>
  );
}
