/**
 * API client for communicating with the FastAPI backend.
 */

import type {
  AgentListItem,
  AgentHierarchy,
  AgentPrompts,
  AgentSummary,
  DomainEvent,
  CategorizedEvents,
  Prompt,
  PromptList,
  PromptUpdate,
  RenderedPrompt,
  PromptVariables,
  SystemConfig,
  ExecutionSummary,
  PaginatedAgentList,
  HierarchyTrace,
  TraceAgentNode,
} from '../types/api';

const API_BASE = '/api';

class ApiError extends Error {
  status: number;
  statusText: string;

  constructor(status: number, statusText: string, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.statusText = statusText;
  }
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${url}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });

  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, response.statusText, text);
  }

  return response.json();
}

// Agent endpoints
export async function listBossAgents(): Promise<AgentListItem[]> {
  const response = await fetchJson<PaginatedAgentList>('/agents');
  return response.items;
}

export async function getAgent(agentId: string): Promise<AgentListItem> {
  return fetchJson<AgentListItem>(`/agents/${agentId}`);
}

export async function getAgentHierarchy(agentId: string): Promise<AgentHierarchy> {
  return fetchJson<AgentHierarchy>(`/agents/${agentId}/hierarchy`);
}

// Event endpoints
export async function getAgentEvents(agentId: string): Promise<CategorizedEvents> {
  return fetchJson<CategorizedEvents>(`/events/${agentId}`);
}

export async function getAllAgentEvents(agentId: string): Promise<DomainEvent[]> {
  return fetchJson<DomainEvent[]>(`/events/${agentId}/all`);
}

export async function getHierarchyEvents(rootId: string): Promise<DomainEvent[]> {
  return fetchJson<DomainEvent[]>(`/events/hierarchy/${rootId}/all`);
}

export function createEventSource(rootId: string): EventSource {
  return new EventSource(`${API_BASE}/events/sse/${rootId}`);
}

// Prompt endpoints
export async function listAllPrompts(): Promise<PromptList> {
  return fetchJson<PromptList>('/prompts');
}

export async function listCategoryPrompts(category: string): Promise<PromptList> {
  return fetchJson<PromptList>(`/prompts/${category}`);
}

export async function getPrompt(category: string, name: string): Promise<Prompt> {
  return fetchJson<Prompt>(`/prompts/${category}/${name}`);
}

export async function updatePrompt(
  category: string,
  name: string,
  update: PromptUpdate,
): Promise<Prompt> {
  return fetchJson<Prompt>(`/prompts/${category}/${name}`, {
    method: 'PUT',
    body: JSON.stringify(update),
  });
}

export async function previewPrompt(category: string, name: string): Promise<RenderedPrompt> {
  return fetchJson<RenderedPrompt>(`/prompts/${category}/${name}/preview`, {
    method: 'POST',
  });
}

export async function resetPrompt(category: string, name: string): Promise<Prompt> {
  return fetchJson<Prompt>(`/prompts/${category}/${name}/reset`, {
    method: 'POST',
  });
}

export async function getTemplateVariables(): Promise<PromptVariables> {
  return fetchJson<PromptVariables>('/prompts/variables');
}

// Agent Summary endpoint
export async function getAgentSummary(agentId: string): Promise<AgentSummary> {
  return fetchJson<AgentSummary>(`/agents/${agentId}/summary`);
}

// System Configuration endpoint
export async function getSystemConfig(): Promise<SystemConfig> {
  return fetchJson<SystemConfig>('/config');
}

// Execution Summary endpoints

/**
 * Get the comprehensive execution summary for an agent hierarchy.
 * Includes cost breakdown, timing, and node counts.
 */
export async function getExecutionSummary(agentId: string): Promise<ExecutionSummary> {
  return fetchJson<ExecutionSummary>(`/agents/${agentId}/execution-summary`);
}

/**
 * Create an EventSource for real-time execution summary updates.
 * The server sends 'summary' events with ExecutionSummary data.
 *
 * @example
 * const source = createSummaryEventSource(rootId);
 * source.addEventListener('summary', (event) => {
 *   const summary = JSON.parse(event.data);
 *   console.log('Updated cost:', summary.cost.total_cost_usd);
 * });
 */
export function createSummaryEventSource(rootId: string): EventSource {
  return new EventSource(`${API_BASE}/events/sse/${rootId}/summary`);
}

// Prompt endpoints

/**
 * Get all prompts sent by an agent.
 * Returns prompts for complexity evaluation, task decomposition, and worker execution.
 */
export async function getAgentPrompts(agentId: string): Promise<AgentPrompts> {
  return fetchJson<AgentPrompts>(`/events/${agentId}/prompts`);
}

// Prompt Trace endpoints

/**
 * Get the complete prompt trace for an agent hierarchy.
 * Returns the full tree structure with all prompts parsed into
 * provenance-tagged sections. Use this for the Prompt Trace Viewer UI.
 */
export async function getHierarchyTrace(rootId: string): Promise<HierarchyTrace> {
  return fetchJson<HierarchyTrace>(`/prompt-trace/trace/${rootId}`);
}

/**
 * Get the prompt trace for a single agent.
 * Fetches only the target agent's events - useful for detail views.
 */
export async function getAgentTrace(agentId: string): Promise<TraceAgentNode> {
  return fetchJson<TraceAgentNode>(`/prompt-trace/agent/${agentId}`);
}
