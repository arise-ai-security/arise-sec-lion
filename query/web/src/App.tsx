/**
 * Main application component for the Arise Agent Dashboard.
 * Provides real-time visualization of agent hierarchy and events.
 */

import { useEffect, useState, useCallback, useRef } from 'react';
import { AgentSidebar } from './components/AgentSidebar';
import { AgentTree } from './components/AgentTree';
import { ConfigPanel } from './components/ConfigPanel';
import { CostPanel } from './components/CostPanel';
import { EventPanel } from './components/EventPanel';
import { PromptsPanel } from './components/PromptsPanel';
import { RegisteredTasksPanel } from './components/RegisteredTasksPanel';
import { SummaryPanel } from './components/SummaryPanel';
import { useSSE } from './hooks/useSSE';
import { useSummarySSE } from './hooks/useSummarySSE';
import * as api from './api/client';
import type { AgentListItem, AgentHierarchy, AgentPrompt, AgentSummary, CategorizedEvents, DomainEvent, RegisteredTask } from './types/api';

function App() {
  const [agents, setAgents] = useState<AgentListItem[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [hierarchy, setHierarchy] = useState<AgentHierarchy | null>(null);
  const [events, setEvents] = useState<CategorizedEvents | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [realtimeEvents, setRealtimeEvents] = useState<DomainEvent[]>([]);
  const [summary, setSummary] = useState<AgentSummary | null>(null);
  const [showConfigPanel, setShowConfigPanel] = useState(false);
  const [rightPanelView, setRightPanelView] = useState<'summary' | 'events' | 'costs' | 'tasks' | 'prompts'>('summary');

  const [loadingAgents, setLoadingAgents] = useState(true);
  const [loadingHierarchy, setLoadingHierarchy] = useState(false);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [loadingSummary, setLoadingSummary] = useState(false);
  const [registeredTasks, setRegisteredTasks] = useState<RegisteredTask[]>([]);
  const [registeredTasksTotal, setRegisteredTasksTotal] = useState(0);
  const [loadingTasks, setLoadingTasks] = useState(false);
  const [nodePrompts, setNodePrompts] = useState<AgentPrompt[]>([]);
  const [nodePromptsTotal, setNodePromptsTotal] = useState(0);
  const [loadingPrompts, setLoadingPrompts] = useState(false);

  // Use refs for values needed in callbacks to avoid dependency loops
  const selectedAgentIdRef = useRef(selectedAgentId);
  selectedAgentIdRef.current = selectedAgentId;

  // Debounce ref for hierarchy refresh
  const refreshDebounceRef = useRef<number | null>(null);

  // Fetch list of BOSS agents - stable callback (no dependencies that change)
  const loadAgents = useCallback(async () => {
    try {
      const data = await api.listBossAgents();
      setAgents(data);

      // Auto-select first agent only if none selected (use ref to avoid loop)
      if (!selectedAgentIdRef.current && data.length > 0) {
        setSelectedAgentId(data[0].id);
        setSelectedNodeId(data[0].id);
      }
    } catch (error) {
      console.error('Failed to load agents:', error);
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

  // Fetch registered tasks when agent is selected
  const loadRegisteredTasks = useCallback(async () => {
    if (!selectedAgentId) {
      setRegisteredTasks([]);
      setRegisteredTasksTotal(0);
      return;
    }

    setLoadingTasks(true);
    try {
      const data = await api.getRegisteredTasks(selectedAgentId);
      setRegisteredTasks(data.tasks);
      setRegisteredTasksTotal(data.total);
    } catch (error) {
      console.error('Failed to load registered tasks:', error);
      setRegisteredTasks([]);
      setRegisteredTasksTotal(0);
    } finally {
      setLoadingTasks(false);
    }
  }, [selectedAgentId]);

  // Load registered tasks when agent changes
  useEffect(() => {
    loadRegisteredTasks();
  }, [loadRegisteredTasks]);

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

  // Debounced hierarchy refresh - stable callback using ref
  const refreshHierarchy = useCallback(() => {
    // Cancel any pending refresh
    if (refreshDebounceRef.current) {
      clearTimeout(refreshDebounceRef.current);
    }

    // Debounce: wait 500ms before actually refreshing
    refreshDebounceRef.current = window.setTimeout(async () => {
      const agentId = selectedAgentIdRef.current;
      if (!agentId) return;

      try {
        const data = await api.getAgentHierarchy(agentId);
        setHierarchy(data);
      } catch (error) {
        console.error('Failed to refresh hierarchy:', error);
      }
    }, 500);
  }, []);

  // Cleanup debounce on unmount
  useEffect(() => {
    return () => {
      if (refreshDebounceRef.current) {
        clearTimeout(refreshDebounceRef.current);
      }
    };
  }, []);

  // Handle real-time events via SSE
  const handleSSEEvent = useCallback((event: DomainEvent) => {
    setRealtimeEvents(prev => {
      // Limit to last 100 events to prevent memory bloat
      const updated = [...prev, event];
      return updated.slice(-100);
    });

    // Refresh hierarchy when structure-changing events occur (debounced)
    const structureEvents = ['AgentCreated', 'ChildSpawned', 'StatusChanged', 'WorkCompleted', 'WorkFailed'];
    if (structureEvents.includes(event.event_type)) {
      refreshHierarchy();
      // Also refresh registered tasks when new agents are created
      if (event.event_type === 'ChildSpawned') {
        loadRegisteredTasks();
      }
    }
  }, [refreshHierarchy, loadRegisteredTasks]);

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
        />
      </div>

      {/* Main content area */}
      <div className="flex-1 flex flex-col">
        {/* Header */}
        <header className="h-14 flex items-center justify-between px-6 bg-white dark:bg-gray-800 border-b dark:border-gray-700">
          <div className="flex items-center">
            <h1 className="text-xl font-bold text-gray-800 dark:text-white">
              Arise Agent Dashboard
            </h1>
            {hierarchy && (
              <span className="ml-4 text-sm text-gray-500">
                {hierarchy.total_agents} agents | depth {hierarchy.depth}
              </span>
            )}
          </div>
          <div className="flex items-center gap-4">
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
        </header>

        {/* Tree view */}
        <div className="flex-1 relative">
          {loadingHierarchy ? (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="animate-pulse text-gray-500">Loading hierarchy...</div>
            </div>
          ) : (
            <AgentTree hierarchy={hierarchy} onNodeClick={handleNodeClick} />
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
              onClick={() => setRightPanelView('tasks')}
              className={`px-3 py-1.5 text-sm rounded-lg transition-colors ${
                rightPanelView === 'tasks'
                  ? 'bg-blue-500 text-white'
                  : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
            >
              Tasks
              {registeredTasksTotal > 0 && (
                <span className="ml-1.5 px-1.5 py-0.5 text-xs bg-purple-600 rounded-full">
                  {registeredTasksTotal}
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
            <SummaryPanel summary={summary} loading={loadingSummary} />
          ) : rightPanelView === 'events' ? (
            <EventPanel events={events} loading={loadingEvents} />
          ) : rightPanelView === 'costs' ? (
            <CostPanel
              summary={executionSummary}
              loading={!executionSummary && !!selectedAgentId}
              isConnected={isSummaryConnected}
            />
          ) : rightPanelView === 'tasks' ? (
            <RegisteredTasksPanel
              tasks={registeredTasks}
              total={registeredTasksTotal}
              loading={loadingTasks}
              onRefresh={loadRegisteredTasks}
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

export default App;
