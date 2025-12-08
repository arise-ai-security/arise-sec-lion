/**
 * TypeScript types matching the FastAPI Pydantic schemas.
 * These mirror the definitions in presentation/api/schemas.py
 */

// Agent types
export type AgentRole = 'BOSS' | 'MANAGER' | 'WORKER' | 'PENDING';
export type AgentStatus = 'INITIALIZING' | 'WORKING' | 'COMPLETED' | 'FAILED';

export interface AgentListItem {
  id: string;
  role: AgentRole;
  status: AgentStatus;
  task_description: string;
  created_at: string | null;
}

export interface AgentNode {
  id: string;
  role: AgentRole;
  status: AgentStatus;
  task_description: string;
  parent_id: string | null;
  children: AgentNode[];
}

export interface AgentHierarchy {
  root: AgentNode;
  total_agents: number;
  depth: number;
}

// Event types
export type OutputType = 'thinking' | 'progress' | 'output' | 'debug';

export interface DomainEvent {
  event_type: string;
  aggregate_id: string;
  sequence_number: number;
  occurred_at: string;
  data: Record<string, unknown>;
}

/** Data for ThoughtCaptured events */
export interface ThoughtCapturedData {
  content: string;
  stream: string;
  output_type: OutputType;
}

export interface CategorizedEvents {
  received: DomainEvent[];
  produced: DomainEvent[];
  passed: DomainEvent[];
  thinking: DomainEvent[];
}

// Prompt types
export interface Prompt {
  category: string;
  name: string;
  content: string;
  updated_at: string | null;
}

export interface PromptList {
  prompts: Prompt[];
  total: number;
}

export interface PromptUpdate {
  content: string;
}

export interface RenderedPrompt {
  rendered: string;
  variables_used: string[];
}

export interface PromptVariables {
  variables: Record<string, string>;
}
