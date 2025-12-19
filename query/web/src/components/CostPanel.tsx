/**
 * Panel for displaying execution cost summary with breakdowns.
 * Shows total cost, cost by role, model, timing, and node counts.
 */

import { useState } from 'react';
import type { ExecutionSummary } from '../types/api';

interface CostPanelProps {
  summary: ExecutionSummary | null;
  loading: boolean;
  isConnected: boolean;
}

function Section({ title, children, defaultOpen = true }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  return (
    <div className="mb-4">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
      >
        <span>{title}</span>
        <svg
          className={`w-4 h-4 transition-transform ${isOpen ? 'rotate-180' : ''}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {isOpen && children}
    </div>
  );
}

function StatCard({ label, value, subValue, colorClass = 'text-gray-800 dark:text-white' }: {
  label: string;
  value: string | number;
  subValue?: string;
  colorClass?: string;
}) {
  return (
    <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded-lg">
      <div className="text-xs text-gray-500 dark:text-gray-400">{label}</div>
      <div className={`text-lg font-semibold ${colorClass}`}>{value}</div>
      {subValue && <div className="text-xs text-gray-500">{subValue}</div>}
    </div>
  );
}

function ProgressBar({ value, max, colorClass = 'bg-blue-500' }: { value: number; max: number; colorClass?: string }) {
  const percentage = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
      <div className={`${colorClass} h-2 rounded-full transition-all`} style={{ width: `${percentage}%` }} />
    </div>
  );
}

function formatCurrency(value: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  }).format(value);
}

function formatDuration(seconds: number): string {
  if (seconds < 60) {
    return `${seconds.toFixed(1)}s`;
  }
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}m ${secs.toFixed(0)}s`;
}

function formatTokens(tokens: number): string {
  if (tokens >= 1000000) {
    return `${(tokens / 1000000).toFixed(2)}M`;
  }
  if (tokens >= 1000) {
    return `${(tokens / 1000).toFixed(1)}K`;
  }
  return tokens.toString();
}

const roleColors: Record<string, string> = {
  BOSS: 'bg-purple-500',
  MANAGER: 'bg-blue-500',
  WORKER: 'bg-green-500',
  PENDING: 'bg-gray-400',
  UNKNOWN: 'bg-gray-300',
};

const roleIcons: Record<string, string> = {
  BOSS: '👑',
  MANAGER: '📋',
  WORKER: '⚙️',
  PENDING: '⏳',
};

export function CostPanel({ summary, loading, isConnected }: CostPanelProps) {
  if (loading) {
    return (
      <div className="p-4 text-gray-500">
        <div className="animate-pulse">Loading cost summary...</div>
      </div>
    );
  }

  if (!summary) {
    return (
      <div className="p-4 text-gray-500">
        Select an agent to view execution costs
      </div>
    );
  }

  const { cost, timing, node_counts, events_by_type, error_count } = summary;
  const totalCost = cost.total_cost_usd;

  // Calculate percentages for role breakdown
  const rolePercentages = Object.entries(cost.cost_by_role)
    .filter(([, value]) => value > 0)
    .sort((a, b) => b[1] - a[1]);

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full">
      {/* Connection status */}
      <div className="flex items-center gap-2 text-xs">
        <span className={`w-2 h-2 rounded-full ${isConnected ? 'bg-green-500' : 'bg-gray-400'}`} />
        <span className="text-gray-500">
          {isConnected ? 'Live updates' : 'Static view'}
        </span>
      </div>

      {/* Total Cost Header */}
      <div className="bg-gradient-to-r from-blue-500 to-purple-500 rounded-lg p-4 text-white">
        <div className="text-sm opacity-80">Total Cost</div>
        <div className="text-3xl font-bold">{formatCurrency(totalCost)}</div>
        <div className="text-sm opacity-80 mt-1">
          {formatTokens(cost.total_tokens)} tokens
        </div>
        {cost.budget_limit_usd && (
          <div className="mt-2">
            <div className="flex justify-between text-xs mb-1">
              <span>Budget</span>
              <span>{formatCurrency(cost.budget_limit_usd)}</span>
            </div>
            <ProgressBar
              value={totalCost}
              max={cost.budget_limit_usd}
              colorClass={cost.budget_exceeded ? 'bg-red-500' : 'bg-white/50'}
            />
            {cost.budget_exceeded && (
              <div className="text-xs text-red-200 mt-1">Budget exceeded!</div>
            )}
          </div>
        )}
      </div>

      {/* Cost Type Breakdown */}
      <Section title="Cost Breakdown">
        <div className="grid grid-cols-2 gap-2">
          <StatCard
            label="LLM Costs"
            value={formatCurrency(cost.llm_cost_usd)}
            subValue={`${formatTokens(cost.prompt_tokens)} in / ${formatTokens(cost.completion_tokens)} out`}
          />
          <StatCard
            label="Worker Costs"
            value={formatCurrency(cost.worker_cost_usd)}
          />
        </div>
      </Section>

      {/* Cost by Role */}
      <Section title="Cost by Role">
        <div className="space-y-2">
          {rolePercentages.map(([role, amount]) => (
            <div key={role} className="flex items-center gap-2">
              <span className="text-lg">{roleIcons[role] || '❓'}</span>
              <div className="flex-1">
                <div className="flex justify-between text-sm mb-1">
                  <span className="text-gray-700 dark:text-gray-300">{role}</span>
                  <span className="font-medium text-gray-800 dark:text-white">
                    {formatCurrency(amount)}
                  </span>
                </div>
                <ProgressBar value={amount} max={totalCost} colorClass={roleColors[role]} />
              </div>
            </div>
          ))}
        </div>
      </Section>

      {/* Cost by Model */}
      {Object.keys(cost.cost_by_model).length > 0 && (
        <Section title="Cost by Model" defaultOpen={false}>
          <div className="space-y-2">
            {Object.entries(cost.cost_by_model)
              .sort((a, b) => b[1] - a[1])
              .map(([model, amount]) => (
                <div key={model} className="flex justify-between text-sm bg-gray-50 dark:bg-gray-800 p-2 rounded">
                  <span className="text-gray-600 dark:text-gray-400 font-mono text-xs">
                    {model}
                  </span>
                  <span className="font-medium text-gray-800 dark:text-white">
                    {formatCurrency(amount)}
                  </span>
                </div>
              ))}
          </div>
        </Section>
      )}

      {/* Cost by Operation */}
      {Object.keys(cost.cost_by_operation).length > 0 && (
        <Section title="Cost by Operation" defaultOpen={false}>
          <div className="space-y-2">
            {Object.entries(cost.cost_by_operation)
              .sort((a, b) => b[1] - a[1])
              .map(([operation, amount]) => (
                <div key={operation} className="flex justify-between text-sm bg-gray-50 dark:bg-gray-800 p-2 rounded">
                  <span className="text-gray-600 dark:text-gray-400">
                    {operation.replace('_', ' ')}
                  </span>
                  <span className="font-medium text-gray-800 dark:text-white">
                    {formatCurrency(amount)}
                  </span>
                </div>
              ))}
          </div>
        </Section>
      )}

      {/* Node Counts */}
      <Section title="Agent Nodes">
        <div className="grid grid-cols-5 gap-2">
          <StatCard label="Total" value={node_counts.total} colorClass="text-blue-600 dark:text-blue-400" />
          <StatCard label="Boss" value={node_counts.BOSS} />
          <StatCard label="Manager" value={node_counts.MANAGER} />
          <StatCard label="Worker" value={node_counts.WORKER} />
          <StatCard label="Pending" value={node_counts.PENDING} />
        </div>
      </Section>

      {/* Execution Timing */}
      <Section title="Execution Time">
        <div className="space-y-2">
          <StatCard
            label="Total Duration"
            value={formatDuration(timing.total_seconds)}
            colorClass="text-green-600 dark:text-green-400"
          />

          {Object.keys(timing.by_role).length > 0 && (
            <div className="mt-2">
              <div className="text-xs text-gray-500 mb-1">Time by Role</div>
              <div className="space-y-1">
                {Object.entries(timing.by_role)
                  .sort((a, b) => b[1] - a[1])
                  .map(([role, seconds]) => (
                    <div key={role} className="flex justify-between text-sm bg-gray-50 dark:bg-gray-800 p-1.5 rounded">
                      <span className="text-gray-600 dark:text-gray-400">{role}</span>
                      <span className="font-medium text-gray-800 dark:text-white">
                        {formatDuration(seconds)}
                      </span>
                    </div>
                  ))}
              </div>
            </div>
          )}

          {Object.keys(timing.by_phase).length > 0 && (
            <div className="mt-2">
              <div className="text-xs text-gray-500 mb-1">Time by Phase</div>
              <div className="space-y-1">
                {Object.entries(timing.by_phase)
                  .sort((a, b) => b[1] - a[1])
                  .map(([phase, seconds]) => (
                    <div key={phase} className="flex justify-between text-sm bg-gray-50 dark:bg-gray-800 p-1.5 rounded">
                      <span className="text-gray-600 dark:text-gray-400 capitalize">{phase}</span>
                      <span className="font-medium text-gray-800 dark:text-white">
                        {formatDuration(seconds)}
                      </span>
                    </div>
                  ))}
              </div>
            </div>
          )}
        </div>
      </Section>

      {/* Event Statistics */}
      <Section title="Event Statistics" defaultOpen={false}>
        <div className="space-y-2">
          <StatCard
            label="Total Events"
            value={summary.total_events}
          />
          {error_count > 0 && (
            <StatCard
              label="Errors"
              value={error_count}
              colorClass="text-red-600 dark:text-red-400"
            />
          )}
          <div className="mt-2 space-y-1">
            {Object.entries(events_by_type)
              .sort((a, b) => b[1] - a[1])
              .map(([type, count]) => (
                <div key={type} className="flex justify-between text-sm bg-gray-50 dark:bg-gray-800 p-1.5 rounded">
                  <span className="text-gray-600 dark:text-gray-400 font-mono text-xs">{type}</span>
                  <span className="font-medium text-gray-800 dark:text-white">{count}</span>
                </div>
              ))}
          </div>
        </div>
      </Section>
    </div>
  );
}
