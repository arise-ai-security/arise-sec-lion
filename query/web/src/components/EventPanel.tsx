/**
 * Panel for displaying agent events with filtering and output type differentiation.
 * Supports auto-scroll to show latest events in real-time.
 */

import { useState, useRef, useEffect } from 'react';
import type { CategorizedEvents, DomainEvent, OutputType, ThoughtCapturedData } from '../types/api';

interface EventPanelProps {
  events: CategorizedEvents | null;
  loading: boolean;
}

type EventCategory = 'all' | 'received' | 'produced' | 'passed' | 'thinking';

const categoryLabels: Record<EventCategory, string> = {
  all: 'All',
  received: 'Received',
  produced: 'Produced',
  passed: 'Passed',
  thinking: 'Thinking',
};

const eventTypeColors: Record<string, string> = {
  AgentCreated: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
  TaskAssigned: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  StatusChanged: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  ComplexityEvaluated: 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300',
  SubtasksDefined: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300',
  ChildSpawned: 'bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-300',
  ChildCompleted: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  ChildFailed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
  CodeGenerationStarted: 'bg-pink-100 text-pink-800 dark:bg-pink-900/30 dark:text-pink-300',
  WorkCompleted: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300',
  WorkFailed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
  ThoughtCaptured: 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300',
  VerificationFailed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
  VerificationPassed: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300',
  DecisionInfeasible: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
  RetryScheduled: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300',
  RedecompositionTriggered: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300',
  ProbeStarted: 'bg-teal-100 text-teal-800 dark:bg-teal-900/30 dark:text-teal-300',
  ProbeCompleted: 'bg-teal-100 text-teal-800 dark:bg-teal-900/30 dark:text-teal-300',
};

/** Color coding by output_type for ThoughtCaptured events */
const outputTypeColors: Record<OutputType, string> = {
  thinking: 'border-l-4 border-l-purple-500 bg-purple-50/50 dark:bg-purple-900/20',
  progress: 'border-l-4 border-l-blue-500 bg-blue-50/50 dark:bg-blue-900/20',
  output: 'border-l-4 border-l-green-500 bg-green-50/50 dark:bg-green-900/20',
  debug: 'border-l-4 border-l-gray-400 bg-gray-50/50 dark:bg-gray-800/50',
};

const outputTypeLabels: Record<OutputType, string> = {
  thinking: '💭 Thinking',
  progress: '⚡ Progress',
  output: '📤 Output',
  debug: '🔧 Debug',
};

const outputTypeBadgeColors: Record<OutputType, string> = {
  thinking: 'bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300',
  progress: 'bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300',
  output: 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300',
  debug: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
};

/** Check if event is a ThoughtCaptured event and extract its data */
function getThoughtData(event: DomainEvent): ThoughtCapturedData | null {
  if (event.event_type !== 'ThoughtCaptured') return null;
  const data = event.data as Partial<ThoughtCapturedData>;
  return {
    content: data.content || '',
    stream: data.stream || 'tool',
    output_type: (data.output_type as OutputType) || 'output',
  };
}

function EventCard({ event, outputTypeFilter }: { event: DomainEvent; outputTypeFilter: Set<OutputType> }) {
  const [expanded, setExpanded] = useState(false);
  const colorClass = eventTypeColors[event.event_type] || 'bg-gray-100 text-gray-800';
  const thoughtData = getThoughtData(event);

  // If this is a ThoughtCaptured event and filtered out, return null
  if (thoughtData && !outputTypeFilter.has(thoughtData.output_type)) {
    return null;
  }

  // Special rendering for ThoughtCaptured events
  if (thoughtData) {
    const outputTypeClass = outputTypeColors[thoughtData.output_type];
    const badgeClass = outputTypeBadgeColors[thoughtData.output_type];

    return (
      <div className={`rounded-lg mb-2 overflow-hidden ${outputTypeClass}`}>
        <div className="px-3 py-2">
          <div className="flex items-center justify-between mb-1">
            <div className="flex items-center gap-2">
              <span className={`px-2 py-0.5 text-xs rounded ${badgeClass}`}>
                {outputTypeLabels[thoughtData.output_type]}
              </span>
              <span className="text-xs text-gray-400">
                {thoughtData.stream}
              </span>
            </div>
            <span className="text-xs text-gray-400">
              {new Date(event.occurred_at).toLocaleTimeString()}
            </span>
          </div>
          <p className="text-sm text-gray-700 dark:text-gray-200 whitespace-pre-wrap font-mono">
            {thoughtData.content}
          </p>
        </div>
      </div>
    );
  }

  // Special rendering for verification events
  if (event.event_type === 'VerificationPassed' || event.event_type === 'VerificationFailed') {
    const passed = event.event_type === 'VerificationPassed';
    const data = event.data as Record<string, unknown>;
    const feedback = (data?.feedback as string) || '';
    const failedStage = (data?.failed_stage as string) || '';
    const stagesPassed = (data?.stages_passed as string[]) || [];

    return (
      <div className={`rounded-lg mb-2 overflow-hidden border-l-4 ${passed ? 'border-l-emerald-500 bg-emerald-50/50 dark:bg-emerald-900/20' : 'border-l-red-500 bg-red-50/50 dark:bg-red-900/20'}`}>
        <div className="px-3 py-2">
          <div className="flex items-center justify-between mb-1">
            <div className="flex items-center gap-2">
              <span className={`px-2 py-0.5 text-xs rounded ${colorClass}`}>
                {passed ? '✅ Verification Passed' : `❌ Verification Failed (${failedStage})`}
              </span>
            </div>
            <span className="text-xs text-gray-400">
              {new Date(event.occurred_at).toLocaleTimeString()}
            </span>
          </div>
          {!passed && stagesPassed.length > 0 && (
            <div className="mb-1">
              <span className="text-xs text-gray-500">Stages passed: </span>
              {stagesPassed.map((s: string) => (
                <span key={s} className="text-xs bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300 rounded px-1 mr-1">{s}</span>
              ))}
            </div>
          )}
          {feedback && (
            <p className={`text-sm whitespace-pre-wrap ${passed ? 'text-emerald-700 dark:text-emerald-300' : 'text-red-700 dark:text-red-300'}`}>
              {feedback}
            </p>
          )}
        </div>
      </div>
    );
  }

  // Special rendering for RetryScheduled events
  if (event.event_type === 'RetryScheduled') {
    const data = event.data as Record<string, unknown>;
    const attempt = data?.attempt as number || 0;
    const reason = (data?.reason as string) || '';
    const escalatedModel = data?.escalated_model as string || '';

    return (
      <div className="rounded-lg mb-2 overflow-hidden border-l-4 border-l-amber-500 bg-amber-50/50 dark:bg-amber-900/20">
        <div className="px-3 py-2">
          <div className="flex items-center justify-between mb-1">
            <div className="flex items-center gap-2">
              <span className={`px-2 py-0.5 text-xs rounded ${colorClass}`}>
                🔄 Retry #{attempt}{escalatedModel ? ` → ${escalatedModel}` : ''}
              </span>
            </div>
            <span className="text-xs text-gray-400">
              {new Date(event.occurred_at).toLocaleTimeString()}
            </span>
          </div>
          {reason && (
            <p className="text-sm text-amber-700 dark:text-amber-300 whitespace-pre-wrap">
              {reason}
            </p>
          )}
        </div>
      </div>
    );
  }

  // Standard rendering for other events
  return (
    <div className="border dark:border-gray-700 rounded-lg mb-2 overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full px-3 py-2 text-left flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-800"
      >
        <div className="flex items-center gap-2">
          <span className={`px-2 py-0.5 text-xs rounded ${colorClass}`}>
            {event.event_type}
          </span>
          <span className="text-xs text-gray-500">
            #{event.sequence_number}
          </span>
        </div>
        <span className="text-xs text-gray-400">
          {new Date(event.occurred_at).toLocaleTimeString()}
        </span>
      </button>
      {expanded && (
        <div className="px-3 py-2 bg-gray-50 dark:bg-gray-800 border-t dark:border-gray-700">
          <pre className="text-xs text-gray-600 dark:text-gray-300 overflow-x-auto">
            {JSON.stringify(event.data, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

export function EventPanel({ events, loading }: EventPanelProps) {
  const [category, setCategory] = useState<EventCategory>('all');
  const [outputTypeFilter, setOutputTypeFilter] = useState<Set<OutputType>>(
    new Set(['thinking', 'progress', 'output', 'debug'])
  );
  const [autoScroll, setAutoScroll] = useState(true);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const prevEventCountRef = useRef(0);

  const toggleOutputType = (type: OutputType) => {
    setOutputTypeFilter((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(type)) {
        newSet.delete(type);
      } else {
        newSet.add(type);
      }
      return newSet;
    });
  };

  // Get total event count for auto-scroll detection
  const getTotalEventCount = () => {
    if (!events) return 0;
    return (events.received?.length || 0) +
           (events.produced?.length || 0) +
           (events.passed?.length || 0) +
           (events.thinking?.length || 0);
  };

  // Auto-scroll to bottom when new events arrive
  useEffect(() => {
    const currentCount = getTotalEventCount();
    const isNewEvent = currentCount > prevEventCountRef.current;
    prevEventCountRef.current = currentCount;

    if (autoScroll && isNewEvent && scrollContainerRef.current) {
      scrollContainerRef.current.scrollTop = scrollContainerRef.current.scrollHeight;
    }
  }, [events, autoScroll]);

  if (loading) {
    return (
      <div className="p-4 text-gray-500">
        <div className="animate-pulse">Loading events...</div>
      </div>
    );
  }

  if (!events) {
    return (
      <div className="p-4 text-gray-500">
        Select an agent to view events
      </div>
    );
  }

  const getFilteredEvents = (): DomainEvent[] => {
    if (category === 'all') {
      return [
        ...events.received,
        ...events.produced,
        ...events.passed,
        ...events.thinking,
      ].sort((a, b) => a.sequence_number - b.sequence_number);
    }
    return events[category] || [];
  };

  const filteredEvents = getFilteredEvents();

  // Check if current filter shows any ThoughtCaptured events
  const hasThoughtEvents = filteredEvents.some(e => e.event_type === 'ThoughtCaptured');

  return (
    <div className="flex flex-col h-full">
      {/* Category filter */}
      <div className="flex gap-1 p-2 border-b dark:border-gray-700 overflow-x-auto">
        {(Object.keys(categoryLabels) as EventCategory[]).map((cat) => (
          <button
            key={cat}
            onClick={() => setCategory(cat)}
            className={`
              px-3 py-1 text-xs rounded-full whitespace-nowrap transition-colors
              ${category === cat
                ? 'bg-blue-500 text-white'
                : 'bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600'
              }
            `}
          >
            {categoryLabels[cat]}
          </button>
        ))}
      </div>

      {/* Output type filter (shown when there are ThoughtCaptured events) */}
      {hasThoughtEvents && (
        <div className="flex gap-1 p-2 border-b dark:border-gray-700 bg-gray-50 dark:bg-gray-900/50 overflow-x-auto">
          <span className="text-xs text-gray-500 mr-1 self-center">Filter:</span>
          {(Object.keys(outputTypeLabels) as OutputType[]).map((type) => (
            <button
              key={type}
              onClick={() => toggleOutputType(type)}
              className={`
                px-2 py-0.5 text-xs rounded whitespace-nowrap transition-colors
                ${outputTypeFilter.has(type)
                  ? outputTypeBadgeColors[type]
                  : 'bg-gray-200 dark:bg-gray-700 text-gray-400 line-through'
                }
              `}
            >
              {outputTypeLabels[type]}
            </button>
          ))}
        </div>
      )}

      {/* Auto-scroll toggle */}
      <div className="flex items-center justify-between px-2 py-1 border-b dark:border-gray-700 bg-gray-50 dark:bg-gray-900/50">
        <span className="text-xs text-gray-500">
          {filteredEvents.length} events
        </span>
        <button
          onClick={() => setAutoScroll(!autoScroll)}
          className={`
            flex items-center gap-1 px-2 py-0.5 text-xs rounded transition-colors
            ${autoScroll
              ? 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300'
              : 'bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-400'
            }
          `}
          title={autoScroll ? 'Auto-scroll enabled' : 'Auto-scroll disabled'}
        >
          <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 14l-7 7m0 0l-7-7m7 7V3" />
          </svg>
          {autoScroll ? 'Live' : 'Paused'}
        </button>
      </div>

      {/* Event list */}
      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto p-2">
        {filteredEvents.length === 0 ? (
          <p className="text-gray-500 text-sm">No events in this category</p>
        ) : (
          filteredEvents.map((event, idx) => (
            <EventCard
              key={`${event.aggregate_id}-${event.sequence_number}-${idx}`}
              event={event}
              outputTypeFilter={outputTypeFilter}
            />
          ))
        )}
      </div>
    </div>
  );
}
