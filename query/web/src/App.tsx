/**
 * Main application component for the Arise Agent Dashboard.
 * Provides real-time visualization of agent hierarchy and events.
 */

import { useEffect, useState, useCallback, useRef, useMemo } from 'react';
import { BrowserRouter, Routes, Route, Link } from 'react-router-dom';
import { AgentSidebar } from './components/AgentSidebar';
import { AgentTree } from './components/AgentTree';
import { ConfigPanel } from './components/ConfigPanel';
import { CostPanel } from './components/CostPanel';
import { EventPanel } from './components/EventPanel';
import { PromptsPanel } from './components/PromptsPanel';
import { SummaryPanel } from './components/SummaryPanel';
import { PromptTracePage } from './pages/PromptTracePage';
import { useSSE } from './hooks/useSSE';
import { useSummarySSE } from './hooks/useSummarySSE';
import * as api from './api/client';
import type { AgentListItem, AgentHierarchy, AgentPrompt, AgentSummary, CategorizedEvents, DomainEvent } from './types/api';

function Dashboard() {
  const [agents, setAgents] = useState<AgentListItem[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [hierarchy, setHierarchy] = useState<AgentHierarchy | null>(null);
  const [events, setEvents] = useState<CategorizedEvents | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [realtimeEvents, setRealtimeEvents] = useState<DomainEvent[]>([]);
  const [nodeSSEEvents, setNodeSSEEvents] = useState<Map<string, DomainEvent[]>>(new Map());
  const [summary, setSummary] = useState<AgentSummary | null>(null);
  const [showConfigPanel, setShowConfigPanel] = useState(false);
  const [rightPanelView, setRightPanelView] = useState<'summary' | 'events' | 'costs' | 'prompts'>('summary');

  // Dark mode state - initialize from localStorage or system preference
  const [isDarkMode, setIsDarkMode] = useState(() => {
    const saved = localStorage.getItem('theme');
    if (saved) return saved === 'dark';
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  });

  // Apply dark mode class to document
  useEffect(() => {
    if (isDarkMode) {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
    localStorage.setItem('theme', isDarkMode ? 'dark' : 'light');
  }, [isDarkMode]);

  const [loadingAgents, setLoadingAgents] = useState(true);
  const [loadingHierarchy, setLoadingHierarchy] = useState(false);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [loadingSummary, setLoadingSummary] = useState(false);
  const [agentsError, setAgentsError] = useState<string | null>(null);
  const [nodePrompts, setNodePrompts] = useState<AgentPrompt[]>([]);
  const [nodePromptsTotal, setNodePromptsTotal] = useState(0);
  const [loadingPrompts, setLoadingPrompts] = useState(false);

  // Use refs for values needed in callbacks to avoid dependency loops
  const selectedAgentIdRef = useRef(selectedAgentId);
  selectedAgentIdRef.current = selectedAgentId;

  // Throttle refs for hierarchy refresh (ensures refresh at least every 1s under load)
  const refreshDebounceRef = useRef<number | null>(null);
  const lastRefreshTimeRef = useRef<number>(0);
  const REFRESH_THROTTLE_MS = 1000; // At least refresh every 1 second

  // Fetch list of BOSS agents - stable callback (no dependencies that change)
  const loadAgents = useCallback(async () => {
    try {
      const data = await api.listBossAgents();
      setAgents(data);
      setAgentsError(null);

      // Auto-select first agent only if none selected (use ref to avoid loop)
      if (!selectedAgentIdRef.current && data.length > 0) {
        setSelectedAgentId(data[0].id);
        setSelectedNodeId(data[0].id);
      }
    } catch (error) {
      console.error('Failed to load agents:', error);
      const message = error instanceof Error ? error.message : 'Failed to connect to API';
      setAgentsError(message);
    } finally {
      setLoadingAgents(false);
    }
  }, []); // Empty deps - uses ref for selectedAgentId

  // Initial load and polling (5 second interval to reduce load)
  useEffect(() => {
    loadAgents();

    // Poll for new agents every 5 seconds
    const pollInterval = setInterval(loadAgents, 5000);

    return () => clearInterval(pollInterval);
  }, [loadAgents]);

  // Fetch hierarchy when agent is selected
  useEffect(() => {
    if (!selectedAgentId) {
      setHierarchy(null);
      return;
    }

    let cancelled = false;

    async function loadHierarchy() {
      setLoadingHierarchy(true);
      try {
        const data = await api.getAgentHierarchy(selectedAgentId!);
        if (!cancelled) {
          setHierarchy(data);
        }
      } catch (error) {
        console.error('Failed to load hierarchy:', error);
        if (!cancelled) {
          setHierarchy(null);
        }
      } finally {
        if (!cancelled) {
          setLoadingHierarchy(false);
        }
      }
    }

    loadHierarchy();

    return () => {
      cancelled = true;
    };
  }, [selectedAgentId]);

  // Fetch events and summary when a node is selected
  useEffect(() => {
    if (!selectedNodeId) {
      setEvents(null);
      setSummary(null);
      return;
    }

    let cancelled = false;

    async function loadEvents() {
      setLoadingEvents(true);
      try {
        const data = await api.getAgentEvents(selectedNodeId!);
        if (!cancelled) {
          setEvents(data);
        }
      } catch (error) {
        console.error('Failed to load events:', error);
        if (!cancelled) {
          setEvents(null);
        }
      } finally {
        if (!cancelled) {
          setLoadingEvents(false);
        }
      }
    }

    async function loadSummary() {
      setLoadingSummary(true);
      try {
        const data = await api.getAgentSummary(selectedNodeId!);
        if (!cancelled) {
          setSummary(data);
        }
      } catch (error) {
        console.error('Failed to load summary:', error);
        if (!cancelled) {
          setSummary(null);
        }
      } finally {
        if (!cancelled) {
          setLoadingSummary(false);
        }
      }
    }

    loadEvents();
    loadSummary();

    return () => {
      cancelled = true;
    };
  }, [selectedNodeId]);

  // Merge API events with SSE events for real-time display
  const mergedEvents = useMemo((): CategorizedEvents | null => {
    if (!events) return null;
    if (!selectedNodeId) return events;

    const sseEvents = nodeSSEEvents.get(selectedNodeId) || [];
    if (sseEvents.length === 0) return events;

    // Get the highest sequence number from API events to filter out duplicates
    const allApiEvents = [...events.received, ...events.produced, ...events.passed, ...events.thinking];
    const maxApiSequence = Math.max(
      ...allApiEvents.map(e => e.sequence_number || 0),
      0
    );

    // Filter SSE events to only include new ones (higher sequence number)
    const newSSEEvents = sseEvents.filter(e => (e.sequence_number || 0) > maxApiSequence);
    if (newSSEEvents.length === 0) return events;

    // Helper function to categorize event type
    const categorizeEvent = (event: DomainEvent): keyof CategorizedEvents => {
      // ThoughtCaptured events go to 'thinking'
      if (event.event_type === 'ThoughtCaptured') return 'thinking';
      // Most events are 'received' by the agent (incoming state changes)
      const receivedTypes = ['AgentCreated', 'TaskAssigned', 'ChildCompleted', 'ChildFailed'];
      if (receivedTypes.includes(event.event_type)) return 'received';
      // Events the agent produces
      const producedTypes = ['StatusChanged', 'ComplexityEvaluated', 'SubtasksDefined', 'ChildSpawned',
                            'WorkCompleted', 'WorkFailed', 'CodeGenerationStarted', 'PromptSent',
                            'TokensConsumed', 'WorkerCostRecorded', 'OperationStarted', 'OperationFinished',
                            'VerificationFailed', 'DecisionInfeasible', 'RetryScheduled',
                            'RedecompositionTriggered', 'ProbeStarted', 'ProbeCompleted'];
      if (producedTypes.includes(event.event_type)) return 'produced';
      // Default to 'passed' for other events
      return 'passed';
    };

    // Create merged result with new SSE events
    const merged: CategorizedEvents = {
      received: [...events.received],
      produced: [...events.produced],
      passed: [...events.passed],
      thinking: [...events.thinking],
    };

    for (const event of newSSEEvents) {
      const category = categorizeEvent(event);
      merged[category] = [...merged[category], event];
    }

    return merged;
  }, [events, selectedNodeId, nodeSSEEvents]);

  // Fetch prompts for selected node
  const loadNodePrompts = useCallback(async () => {
    if (!selectedNodeId) {
      setNodePrompts([]);
      setNodePromptsTotal(0);
      return;
    }

    setLoadingPrompts(true);
    try {
      const data = await api.getAgentPrompts(selectedNodeId);
      setNodePrompts(data.prompts);
      setNodePromptsTotal(data.total);
    } catch (error) {
      console.error('Failed to load prompts:', error);
      setNodePrompts([]);
      setNodePromptsTotal(0);
    } finally {
      setLoadingPrompts(false);
    }
  }, [selectedNodeId]);

  // Load prompts when node changes
  useEffect(() => {
    loadNodePrompts();
  }, [loadNodePrompts]);

  // Throttled hierarchy refresh - ensures refresh at least every REFRESH_THROTTLE_MS
  // even when continuous events are coming in
  const refreshHierarchy = useCallback(() => {
    const now = Date.now();
    const timeSinceLastRefresh = now - lastRefreshTimeRef.current;

    // Cancel any pending debounced refresh
    if (refreshDebounceRef.current) {
      clearTimeout(refreshDebounceRef.current);
    }

    const doRefresh = async () => {
      const agentId = selectedAgentIdRef.current;
      if (!agentId) return;

      lastRefreshTimeRef.current = Date.now();
      try {
        const data = await api.getAgentHierarchy(agentId);
        setHierarchy(data);
      } catch (error) {
        console.error('Failed to refresh hierarchy:', error);
      }
    };

    // If enough time has passed, refresh immediately (throttle leading edge)
    if (timeSinceLastRefresh >= REFRESH_THROTTLE_MS) {
      doRefresh();
    } else {
      // Otherwise, schedule a refresh for when the throttle period ends
      const delay = REFRESH_THROTTLE_MS - timeSinceLastRefresh;
      refreshDebounceRef.current = window.setTimeout(doRefresh, delay);
    }
  }, []);

  // Throttle refs for summary/events refresh
  const summaryRefreshRef = useRef<number | null>(null);
  const lastSummaryRefreshRef = useRef<number>(0);
  const SUMMARY_THROTTLE_MS = 500; // Refresh summary at most every 500ms

  // Cleanup throttle timers on unmount
  useEffect(() => {
    return () => {
      if (refreshDebounceRef.current) {
        clearTimeout(refreshDebounceRef.current);
      }
      if (summaryRefreshRef.current) {
        clearTimeout(summaryRefreshRef.current);
      }
    };
  }, []);

  // Throttled refresh for summary and events when events arrive for selected node
  const refreshSelectedNodeData = useCallback((agentId: string) => {
    const now = Date.now();
    const timeSinceLastRefresh = now - lastSummaryRefreshRef.current;

    if (summaryRefreshRef.current) {
      clearTimeout(summaryRefreshRef.current);
    }

    const doRefresh = async () => {
      lastSummaryRefreshRef.current = Date.now();
      try {
        const [summaryData, eventsData] = await Promise.all([
          api.getAgentSummary(agentId),
          api.getAgentEvents(agentId),
        ]);
        setSummary(summaryData);
        setEvents(eventsData);
      } catch (error) {
        console.error('Failed to refresh selected node data:', error);
      }
    };

    if (timeSinceLastRefresh >= SUMMARY_THROTTLE_MS) {
      doRefresh();
    } else {
      const delay = SUMMARY_THROTTLE_MS - timeSinceLastRefresh;
      summaryRefreshRef.current = window.setTimeout(doRefresh, delay);
    }
  }, []);

  // Ref for selectedNodeId to avoid re-creating handleSSEEvent
  const selectedNodeIdRef = useRef(selectedNodeId);
  selectedNodeIdRef.current = selectedNodeId;

  // Handle real-time events via SSE
  const handleSSEEvent = useCallback((event: DomainEvent) => {
    setRealtimeEvents(prev => {
      // Limit to last 100 events to prevent memory bloat
      const updated = [...prev, event];
      return updated.slice(-100);
    });

    // Append to node-specific SSE events for real-time display
    setNodeSSEEvents(prev => {
      const nodeId = event.aggregate_id;
      const existing = prev.get(nodeId) || [];
      const newMap = new Map(prev);
      // Limit to last 100 events per node
      newMap.set(nodeId, [...existing, event].slice(-100));
      return newMap;
    });

    // Refresh hierarchy when structure-changing events occur (throttled)
    const structureEvents = [
      'AgentCreated', 'ChildSpawned', 'StatusChanged', 'WorkCompleted', 'WorkFailed',
      'VerificationFailed', 'DecisionInfeasible', 'RetryScheduled', 'RedecompositionTriggered',
    ];
    if (structureEvents.includes(event.event_type)) {
      refreshHierarchy();
    }

    // Refresh summary when events arrive for the currently selected node (throttled)
    // Note: We no longer refresh events from API since we merge SSE events directly
    const currentNodeId = selectedNodeIdRef.current;
    if (currentNodeId && event.aggregate_id === currentNodeId) {
      refreshSelectedNodeData(currentNodeId);
    }
  }, [refreshHierarchy, refreshSelectedNodeData]);

  // SSE connection for real-time updates
  const { isConnected } = useSSE({
    rootId: selectedAgentId,
    onEvent: handleSSEEvent,
    onError: (error) => console.error('SSE error:', error),
  });

  // SSE connection for real-time execution summary (costs, timing, node counts)
  const { summary: executionSummary, isConnected: isSummaryConnected } = useSummarySSE({
    rootId: selectedAgentId,
  });

  const handleAgentSelect = useCallback((agentId: string) => {
    setSelectedAgentId(agentId);
    setSelectedNodeId(agentId);
    setRealtimeEvents([]); // Clear realtime events for new selection
    setNodeSSEEvents(new Map()); // Clear node-specific SSE events
  }, []);

  const handleNodeClick = useCallback((nodeId: string) => {
    setSelectedNodeId(nodeId);
  }, []);

  return (
    <div className="flex h-screen bg-gray-50 dark:bg-gray-900">
      {/* Sidebar */}
      <div className="w-64 flex-shrink-0 bg-white dark:bg-gray-800 border-r dark:border-gray-700">
        <AgentSidebar
          agents={agents}
          selectedId={selectedAgentId}
          onSelect={handleAgentSelect}
          loading={loadingAgents}
          error={agentsError}
        />
      </div>

      {/* Main content area */}
      <div className="flex-1 flex flex-col">
        {/* Header */}
        <header className="px-6 py-3 bg-white dark:bg-gray-800 border-b dark:border-gray-700">
          <div className="flex flex-wrap items-center justify-between gap-2">
            {/* Left side - Run info */}
            <div className="flex flex-wrap items-center gap-3 text-sm">
              {hierarchy && (
                <span className="text-gray-500">
                  {hierarchy.total_agents} agents | depth {hierarchy.depth}
                </span>
              )}
              {selectedAgentId && (
                <>
                  <div className="flex items-center gap-1">
                    <span className="text-gray-500">Run:</span>
                    <code className="px-2 py-0.5 bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300 rounded text-xs font-mono">
                      {selectedAgentId}
                    </code>
                  </div>
                  {agents.find(a => a.id === selectedAgentId)?.domain_metadata?.instance_id && (
                    <div className="flex items-center gap-1">
                      <span className="text-gray-500">Instance:</span>
                      <span className="px-2 py-0.5 bg-green-100 dark:bg-green-900 text-green-700 dark:text-green-300 rounded text-xs">
                        {agents.find(a => a.id === selectedAgentId)?.domain_metadata?.instance_id}
                      </span>
                    </div>
                  )}
                </>
              )}
            </div>
            {/* Right side - Actions */}
            <div className="flex items-center gap-4">
            {/* Dark/Light mode toggle */}
            <button
              onClick={() => setIsDarkMode(!isDarkMode)}
              className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
              title={isDarkMode ? 'Switch to light mode' : 'Switch to dark mode'}
            >
              {isDarkMode ? (
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
                </svg>
              ) : (
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
                </svg>
              )}
            </button>
            {/* Prompt Trace link */}
            {selectedAgentId && (
              <Link
                to={`/prompt-trace/${selectedAgentId}`}
                className="px-3 py-1.5 text-xs bg-purple-100 dark:bg-purple-900 hover:bg-purple-200 dark:hover:bg-purple-800 text-purple-700 dark:text-purple-300 rounded-lg flex items-center gap-1.5 transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
                Prompt Trace
              </Link>
            )}
            <button
              onClick={() => setShowConfigPanel(true)}
              className="px-3 py-1.5 text-xs bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-300 rounded-lg flex items-center gap-1.5 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
              </svg>
              Config
            </button>
            <div className="flex items-center gap-2">
              <span
                className={`w-2 h-2 rounded-full ${isConnected ? 'bg-green-500' : 'bg-red-500'}`}
                title={isConnected ? 'Connected' : 'Disconnected'}
              />
              <span className="text-xs text-gray-500">
                {isConnected ? 'Live' : 'Offline'}
              </span>
            </div>
            </div>
          </div>
        </header>

        {/* Tree view */}
        <div className="flex-1 relative">
          {loadingHierarchy ? (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="animate-pulse text-gray-500">Loading hierarchy...</div>
            </div>
          ) : (
            <AgentTree hierarchy={hierarchy} onNodeClick={handleNodeClick} hasAgents={agents.length > 0} />
          )}
        </div>
      </div>

      {/* Right panel with tabs */}
      <div className="w-96 flex-shrink-0 bg-white dark:bg-gray-800 border-l dark:border-gray-700">
        {/* Tab header */}
        <div className="h-14 flex items-center justify-between px-4 border-b dark:border-gray-700">
          <div className="flex gap-1 overflow-x-auto">
            <button
              onClick={() => setRightPanelView('summary')}
              className={`px-3 py-1.5 text-sm rounded-lg transition-colors ${
                rightPanelView === 'summary'
                  ? 'bg-blue-500 text-white'
                  : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
            >
              Summary
            </button>
            <button
              onClick={() => setRightPanelView('events')}
              className={`px-3 py-1.5 text-sm rounded-lg transition-colors ${
                rightPanelView === 'events'
                  ? 'bg-blue-500 text-white'
                  : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
            >
              Events
              {realtimeEvents.length > 0 && (
                <span className="ml-1.5 px-1.5 py-0.5 text-xs bg-blue-600 rounded-full">
                  +{realtimeEvents.length}
                </span>
              )}
            </button>
            <button
              onClick={() => setRightPanelView('costs')}
              className={`px-3 py-1.5 text-sm rounded-lg transition-colors ${
                rightPanelView === 'costs'
                  ? 'bg-blue-500 text-white'
                  : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
            >
              Costs
              {executionSummary && executionSummary.cost.total_cost_usd > 0 && (
                <span className="ml-1.5 px-1.5 py-0.5 text-xs bg-green-600 rounded-full">
                  ${executionSummary.cost.total_cost_usd.toFixed(2)}
                </span>
              )}
            </button>
            <button
              onClick={() => setRightPanelView('prompts')}
              className={`px-3 py-1.5 text-sm rounded-lg transition-colors ${
                rightPanelView === 'prompts'
                  ? 'bg-blue-500 text-white'
                  : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
            >
              Prompts
              {nodePromptsTotal > 0 && (
                <span className="ml-1.5 px-1.5 py-0.5 text-xs bg-orange-600 rounded-full">
                  {nodePromptsTotal}
                </span>
              )}
            </button>
          </div>
        </div>

        {/* Tab content */}
        <div className="h-[calc(100vh-3.5rem)] overflow-hidden">
          {rightPanelView === 'summary' ? (
            <SummaryPanel
              summary={summary}
              loading={loadingSummary}
              workerOutput={mergedEvents?.thinking || []}
            />
          ) : rightPanelView === 'events' ? (
            <EventPanel events={mergedEvents} loading={loadingEvents} />
          ) : rightPanelView === 'costs' ? (
            <CostPanel
              summary={executionSummary}
              loading={!executionSummary && !!selectedAgentId}
              isConnected={isSummaryConnected}
            />
          ) : (
            <PromptsPanel
              prompts={nodePrompts}
              total={nodePromptsTotal}
              loading={loadingPrompts}
              onRefresh={loadNodePrompts}
            />
          )}
        </div>
      </div>

      {/* Config Modal */}
      <ConfigPanel isOpen={showConfigPanel} onClose={() => setShowConfigPanel(false)} />
    </div>
  );
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/prompt-trace/:rootId" element={<PromptTracePage />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
