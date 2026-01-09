/**
 * Main page component for the Prompt Trace Viewer.
 *
 * Features:
 * - Header with back link, title, agent selector
 * - Provenance legend bar
 * - View mode toggle (tree/detail)
 * - Content area: TraceTreeView or AgentDetailView
 */

import { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { getHierarchyTrace } from '../api/client';
import type { HierarchyTrace, TraceAgentNode } from '../types/api';
import { TraceTreeView } from '../components/prompt-trace/TraceTreeView';
import { AgentDetailView } from '../components/prompt-trace/AgentDetailView';

type ViewMode = 'tree' | 'detail';

function findAgentInTree(node: TraceAgentNode, targetId: string): TraceAgentNode | null {
  if (node.agent_id === targetId) {
    return node;
  }
  for (const child of node.children) {
    const found = findAgentInTree(child, targetId);
    if (found) return found;
  }
  return null;
}

export function PromptTracePage() {
  const { rootId } = useParams<{ rootId: string }>();
  const navigate = useNavigate();

  const [trace, setTrace] = useState<HierarchyTrace | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>('tree');
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);

  // Fetch trace data
  useEffect(() => {
    if (!rootId) {
      setError('No root ID provided');
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);

    getHierarchyTrace(rootId)
      .then((data) => {
        setTrace(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message ?? 'Failed to load trace');
        setLoading(false);
      });
  }, [rootId]);

  // Handle node click in tree view
  const handleNodeClick = useCallback((agentId: string) => {
    setSelectedAgentId(agentId);
    setViewMode('detail');
  }, []);

  // Handle back to tree view
  const handleBackToTree = useCallback(() => {
    setViewMode('tree');
  }, []);

  // Get selected agent node
  const selectedAgent = trace && selectedAgentId
    ? findAgentInTree(trace.root, selectedAgentId)
    : null;

  return (
    <div className="h-screen flex flex-col bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-6 py-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            {/* Back to dashboard */}
            <Link
              to="/"
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
                  d="M10 19l-7-7m0 0l7-7m-7 7h18"
                />
              </svg>
              <span>Dashboard</span>
            </Link>

            <div className="h-6 w-px bg-gray-300" />

            <h1 className="text-xl font-bold text-gray-800">Prompt Trace Viewer</h1>

            {trace && (
              <span className="text-sm text-gray-500">
                {trace.total_agents} agents, depth {trace.max_depth}
              </span>
            )}
          </div>

          {/* View mode toggle */}
          {trace && (
            <div className="flex gap-1">
              <button
                onClick={() => setViewMode('tree')}
                className={`px-4 py-2 text-sm rounded-l-lg border ${
                  viewMode === 'tree'
                    ? 'bg-blue-600 text-white border-blue-600'
                    : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50'
                }`}
              >
                Tree View
              </button>
              <button
                onClick={() => setViewMode('detail')}
                disabled={!selectedAgentId}
                className={`px-4 py-2 text-sm rounded-r-lg border ${
                  viewMode === 'detail'
                    ? 'bg-blue-600 text-white border-blue-600'
                    : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed'
                }`}
              >
                Detail View
              </button>
            </div>
          )}
        </div>
      </header>

      {/* Provenance Legend - 3-Layer Architecture */}
      <div className="bg-white border-b border-gray-200 px-6 py-2">
        <div className="flex items-center gap-6 overflow-x-auto text-xs">
          {/* Layer 1: Core Behavior */}
          <div className="flex items-center gap-2">
            <span className="text-gray-400 font-medium">L1:</span>
            <span className="font-medium text-blue-700" title="Agent behavior templates (core/roles/*.j2)">📘 Template</span>
          </div>

          <div className="h-4 w-px bg-gray-300" />

          {/* Layer 2: Shared Context */}
          <div className="flex items-center gap-2">
            <span className="text-gray-400 font-medium">L2:</span>
            <span className="font-medium text-green-700" title="Context passed down from parent agent">🌲 Parent</span>
            <span className="font-medium text-yellow-700" title="Context shared between sibling agents">🔗 Sibling</span>
            <span className="font-medium text-purple-700" title="Results and outcomes from child agents">📤 Children</span>
            <span className="font-medium text-orange-600" title="Global shared decisions and artifacts">🌐 Shared</span>
          </div>

          <div className="h-4 w-px bg-gray-300" />

          {/* Layer 3: Domain-Specific */}
          <div className="flex items-center gap-2">
            <span className="text-gray-400 font-medium">L3:</span>
            <span className="font-medium text-gray-600" title="SEC-bench CVE data, environment, constraints">🔒 Domain</span>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <main className="flex-1 overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-full">
            <div className="text-center">
              <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto mb-4" />
              <p className="text-gray-500">Loading trace data...</p>
            </div>
          </div>
        ) : error ? (
          <div className="flex items-center justify-center h-full">
            <div className="text-center">
              <div className="text-red-500 text-6xl mb-4">!</div>
              <p className="text-red-600 font-medium">{error}</p>
              <button
                onClick={() => navigate('/')}
                className="mt-4 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
              >
                Back to Dashboard
              </button>
            </div>
          </div>
        ) : trace ? (
          viewMode === 'tree' ? (
            <TraceTreeView
              root={trace.root}
              selectedAgentId={selectedAgentId ?? undefined}
              onNodeClick={handleNodeClick}
            />
          ) : selectedAgent ? (
            <AgentDetailView
              agent={selectedAgent}
              onBack={handleBackToTree}
            />
          ) : (
            <div className="flex items-center justify-center h-full text-gray-500">
              Select an agent from the tree to view details.
            </div>
          )
        ) : null}
      </main>
    </div>
  );
}
