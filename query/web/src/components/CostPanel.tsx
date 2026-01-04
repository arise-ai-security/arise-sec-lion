/**
 * Panel for displaying execution cost summary with breakdowns.
 * Shows total cost, cost by role, model, timing, and node counts.
 * Optimized for information density and query performance awareness.
 */

import { useState, useMemo } from 'react';
import type { ExecutionSummary } from '../types/api';

interface CostPanelProps {
  summary: ExecutionSummary | null;
  loading: boolean;
  isConnected: boolean;
}

function Section({ title, children, defaultOpen = true, badge }: { title: string; children: React.ReactNode; defaultOpen?: boolean; badge?: string | number }) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  return (
    <div className="mb-3">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1.5 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
      >
        <span className="flex items-center gap-2">
          {title}
          {badge !== undefined && (
            <span className="px-1.5 py-0.5 text-[10px] bg-gray-200 dark:bg-gray-700 rounded font-normal normal-case">
              {badge}
            </span>
          )}
        </span>
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

function MiniStat({ label, value, colorClass }: { label: string; value: string | number; colorClass?: string }) {
  return (
    <div className="flex flex-col items-center p-2 bg-gray-50 dark:bg-gray-800 rounded">
      <span className={`text-sm font-semibold ${colorClass || 'text-gray-800 dark:text-white'}`}>{value}</span>
      <span className="text-[10px] text-gray-500 dark:text-gray-400 uppercase">{label}</span>
    </div>
  );
}

function ProgressBar({ value, max, colorClass = 'bg-blue-500', showPercent = false }: { value: number; max: number; colorClass?: string; showPercent?: boolean }) {
  const percentage = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
        <div className={`${colorClass} h-1.5 rounded-full transition-all`} style={{ width: `${percentage}%` }} />
      </div>
      {showPercent && <span className="text-[10px] text-gray-500 w-8 text-right">{percentage.toFixed(0)}%</span>}
    </div>
  );
}

function formatCurrency(value: number, compact = false): string {
  if (compact) {
    if (value >= 1) {
      return `$${value.toFixed(2)}`;
    }
    if (value >= 0.01) {
      return `$${value.toFixed(3)}`;
    }
    return `$${value.toFixed(4)}`;
  }
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  }).format(value);
}

function formatDuration(seconds: number, compact = false): string {
  if (seconds < 60) {
    return compact ? `${seconds.toFixed(0)}s` : `${seconds.toFixed(1)}s`;
  }
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}m ${secs.toFixed(0)}s`;
}

function formatTokens(tokens: number, compact = false): string {
  if (tokens >= 1000000) {
    return `${(tokens / 1000000).toFixed(compact ? 1 : 2)}M`;
  }
  if (tokens >= 1000) {
    return `${(tokens / 1000).toFixed(compact ? 0 : 1)}K`;
  }
  return tokens.toString();
}

function formatRate(value: number): string {
  if (value >= 1000) {
    return `$${(value / 1000).toFixed(2)}K`;
  }
  return `$${value.toFixed(2)}`;
}

const roleColors: Record<string, string> = {
  BOSS: 'bg-purple-500',
  MANAGER: 'bg-blue-500',
  WORKER: 'bg-green-500',
  PENDING: 'bg-gray-400',
  UNKNOWN: 'bg-gray-300',
};

const roleTextColors: Record<string, string> = {
  BOSS: 'text-purple-600 dark:text-purple-400',
  MANAGER: 'text-blue-600 dark:text-blue-400',
  WORKER: 'text-green-600 dark:text-green-400',
  PENDING: 'text-gray-600 dark:text-gray-400',
  UNKNOWN: 'text-gray-500',
};

const roleIcons: Record<string, string> = {
  BOSS: '👑',
  MANAGER: '📋',
  WORKER: '⚙️',
  PENDING: '⏳',
};

export function CostPanel({ summary, loading, isConnected }: CostPanelProps) {
  // Compute derived metrics for efficiency analysis
  const metrics = useMemo(() => {
    if (!summary) return null;

    const { cost, timing, node_counts } = summary;
    const totalAgents = node_counts.total;
    const completedAgents = node_counts.WORKER + node_counts.MANAGER; // Active roles

    return {
      costPerAgent: totalAgents > 0 ? cost.total_cost_usd / totalAgents : 0,
      costPerToken: cost.total_tokens > 0 ? (cost.total_cost_usd / cost.total_tokens) * 1000000 : 0, // Per 1M tokens
      tokensPerSecond: timing.total_seconds > 0 ? cost.total_tokens / timing.total_seconds : 0,
      llmRatio: cost.total_cost_usd > 0 ? (cost.llm_cost_usd / cost.total_cost_usd) * 100 : 0,
      workerRatio: cost.total_cost_usd > 0 ? (cost.worker_cost_usd / cost.total_cost_usd) * 100 : 0,
      avgTimePerAgent: completedAgents > 0 ? timing.total_seconds / completedAgents : 0,
    };
  }, [summary]);

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
    <div className="p-3 space-y-3 overflow-y-auto h-full">
      {/* Compact Header: Status + Total Cost */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span className={`w-1.5 h-1.5 rounded-full ${isConnected ? 'bg-green-500' : 'bg-gray-400'}`} />
          <span className="text-[10px] text-gray-500">{isConnected ? 'Live' : 'Static'}</span>
        </div>
        {summary.is_complete && (
          <span className="px-1.5 py-0.5 text-[10px] bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400 rounded">
            Complete
          </span>
        )}
      </div>

      {/* Hero Cost Card - Compact */}
      <div className="bg-gradient-to-r from-blue-500 to-purple-500 rounded-lg p-3 text-white">
        <div className="flex items-baseline justify-between">
          <div>
            <div className="text-2xl font-bold">{formatCurrency(totalCost)}</div>
            <div className="text-xs opacity-80">{formatTokens(cost.total_tokens)} tokens</div>
          </div>
          <div className="text-right">
            <div className="text-lg font-semibold">{formatDuration(timing.total_seconds, true)}</div>
            <div className="text-xs opacity-80">{node_counts.total} agents</div>
          </div>
        </div>
        {cost.budget_limit_usd && (
          <div className="mt-2">
            <div className="flex justify-between text-[10px] mb-0.5">
              <span>Budget</span>
              <span>{formatCurrency(cost.budget_limit_usd, true)} ({((totalCost / cost.budget_limit_usd) * 100).toFixed(0)}%)</span>
            </div>
            <ProgressBar
              value={totalCost}
              max={cost.budget_limit_usd}
              colorClass={cost.budget_exceeded ? 'bg-red-400' : 'bg-white/50'}
            />
          </div>
        )}
      </div>

      {/* Quick Stats Grid */}
      <div className="grid grid-cols-4 gap-1.5">
        <MiniStat label="LLM" value={formatCurrency(cost.llm_cost_usd, true)} colorClass="text-blue-600 dark:text-blue-400" />
        <MiniStat label="Worker" value={formatCurrency(cost.worker_cost_usd, true)} colorClass="text-green-600 dark:text-green-400" />
        <MiniStat label="In" value={formatTokens(cost.prompt_tokens, true)} />
        <MiniStat label="Out" value={formatTokens(cost.completion_tokens, true)} />
      </div>

      {/* Efficiency Metrics */}
      {metrics && (
        <div className="bg-gray-50 dark:bg-gray-800 rounded p-2">
          <div className="text-[10px] text-gray-500 dark:text-gray-400 uppercase mb-1.5">Efficiency</div>
          <div className="grid grid-cols-3 gap-2 text-xs">
            <div>
              <span className="text-gray-500">$/agent:</span>
              <span className="ml-1 font-medium">{formatCurrency(metrics.costPerAgent, true)}</span>
            </div>
            <div>
              <span className="text-gray-500">$/1Mtok:</span>
              <span className="ml-1 font-medium">{formatRate(metrics.costPerToken)}</span>
            </div>
            <div>
              <span className="text-gray-500">tok/s:</span>
              <span className="ml-1 font-medium">{metrics.tokensPerSecond.toFixed(0)}</span>
            </div>
          </div>
        </div>
      )}

      {/* Cost by Role - Compact Inline */}
      <Section title="By Role" badge={rolePercentages.length}>
        <div className="space-y-1.5">
          {rolePercentages.map(([role, amount]) => (
            <div key={role} className="flex items-center gap-2">
              <span className="text-sm w-5">{roleIcons[role] || '❓'}</span>
              <span className={`text-xs w-16 ${roleTextColors[role]}`}>{role}</span>
              <div className="flex-1">
                <ProgressBar value={amount} max={totalCost} colorClass={roleColors[role]} showPercent />
              </div>
              <span className="text-xs font-medium w-16 text-right">{formatCurrency(amount, true)}</span>
            </div>
          ))}
        </div>
      </Section>

      {/* Agent Counts - Ultra Compact */}
      <Section title="Agents" badge={node_counts.total}>
        <div className="flex gap-1.5">
          {[
            { role: 'BOSS', count: node_counts.BOSS, color: 'bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400' },
            { role: 'MANAGER', count: node_counts.MANAGER, color: 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400' },
            { role: 'WORKER', count: node_counts.WORKER, color: 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400' },
            { role: 'PENDING', count: node_counts.PENDING, color: 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-400' },
          ].filter(r => r.count > 0).map(({ role, count, color }) => (
            <div key={role} className={`flex-1 px-2 py-1.5 rounded text-center ${color}`}>
              <div className="text-sm font-semibold">{count}</div>
              <div className="text-[9px] uppercase">{role}</div>
            </div>
          ))}
        </div>
      </Section>

      {/* Cost by Model - Compact */}
      {Object.keys(cost.cost_by_model).length > 0 && (
        <Section title="By Model" badge={Object.keys(cost.cost_by_model).length} defaultOpen={false}>
          <div className="space-y-1">
            {Object.entries(cost.cost_by_model)
              .sort((a, b) => b[1] - a[1])
              .map(([model, amount]) => (
                <div key={model} className="flex items-center gap-2 text-xs bg-gray-50 dark:bg-gray-800 p-1.5 rounded">
                  <span className="flex-1 text-gray-600 dark:text-gray-400 font-mono text-[10px] truncate" title={model}>
                    {model.length > 25 ? model.slice(0, 25) + '...' : model}
                  </span>
                  <span className="font-medium">{formatCurrency(amount, true)}</span>
                  <span className="text-gray-400 w-8 text-right">{((amount / totalCost) * 100).toFixed(0)}%</span>
                </div>
              ))}
          </div>
        </Section>
      )}

      {/* Cost by Operation - Compact */}
      {Object.keys(cost.cost_by_operation).length > 0 && (
        <Section title="By Operation" badge={Object.keys(cost.cost_by_operation).length} defaultOpen={false}>
          <div className="space-y-1">
            {Object.entries(cost.cost_by_operation)
              .sort((a, b) => b[1] - a[1])
              .map(([operation, amount]) => (
                <div key={operation} className="flex items-center justify-between text-xs bg-gray-50 dark:bg-gray-800 p-1.5 rounded">
                  <span className="text-gray-600 dark:text-gray-400 capitalize">
                    {operation.replace(/_/g, ' ')}
                  </span>
                  <span className="font-medium">{formatCurrency(amount, true)}</span>
                </div>
              ))}
          </div>
        </Section>
      )}

      {/* Timing - Compact */}
      <Section title="Timing" badge={formatDuration(timing.total_seconds, true)} defaultOpen={false}>
        <div className="space-y-2">
          {Object.keys(timing.by_role).length > 0 && (
            <div>
              <div className="text-[10px] text-gray-500 mb-1">By Role</div>
              <div className="space-y-1">
                {Object.entries(timing.by_role)
                  .sort((a, b) => b[1] - a[1])
                  .map(([role, seconds]) => (
                    <div key={role} className="flex items-center gap-2 text-xs">
                      <span className={`w-14 ${roleTextColors[role]}`}>{role}</span>
                      <div className="flex-1">
                        <ProgressBar value={seconds} max={timing.total_seconds} colorClass={roleColors[role]} />
                      </div>
                      <span className="font-medium w-12 text-right">{formatDuration(seconds, true)}</span>
                    </div>
                  ))}
              </div>
            </div>
          )}

          {Object.keys(timing.by_phase).length > 0 && (
            <div>
              <div className="text-[10px] text-gray-500 mb-1">By Phase</div>
              <div className="space-y-1">
                {Object.entries(timing.by_phase)
                  .sort((a, b) => b[1] - a[1])
                  .map(([phase, seconds]) => (
                    <div key={phase} className="flex justify-between text-xs bg-gray-50 dark:bg-gray-800 p-1 rounded">
                      <span className="text-gray-600 dark:text-gray-400 capitalize">{phase.replace(/_/g, ' ')}</span>
                      <span className="font-medium">{formatDuration(seconds, true)}</span>
                    </div>
                  ))}
              </div>
            </div>
          )}
        </div>
      </Section>

      {/* Events - Ultra Compact */}
      <Section title="Events" badge={summary.total_events} defaultOpen={false}>
        <div className="space-y-1.5">
          {error_count > 0 && (
            <div className="flex items-center justify-between text-xs bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 p-1.5 rounded">
              <span>Errors</span>
              <span className="font-medium">{error_count}</span>
            </div>
          )}
          <div className="grid grid-cols-2 gap-1">
            {Object.entries(events_by_type)
              .sort((a, b) => b[1] - a[1])
              .slice(0, 8)
              .map(([type, count]) => (
                <div key={type} className="flex justify-between text-[10px] bg-gray-50 dark:bg-gray-800 px-1.5 py-1 rounded">
                  <span className="text-gray-500 truncate mr-1" title={type}>{type}</span>
                  <span className="font-medium text-gray-700 dark:text-gray-300">{count}</span>
                </div>
              ))}
          </div>
          {Object.keys(events_by_type).length > 8 && (
            <div className="text-[10px] text-gray-500 text-center">
              +{Object.keys(events_by_type).length - 8} more event types
            </div>
          )}
        </div>
      </Section>
    </div>
  );
}
