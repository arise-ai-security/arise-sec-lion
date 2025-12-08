/**
 * API client for communicating with the FastAPI backend.
 */

import type {
  AgentListItem,
  AgentHierarchy,
  DomainEvent,
  CategorizedEvents,
  Prompt,
  PromptList,
  PromptUpdate,
  RenderedPrompt,
  PromptVariables,
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
  return fetchJson<AgentListItem[]>('/agents');
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
