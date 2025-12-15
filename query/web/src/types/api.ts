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

// Agent Summary types (CQRS Projection)

export interface SubtaskSummary {
  description: string;
  child_id: string | null;
  child_status: string | null;
}

export interface TaskQueueItem {
  description: string;
  priority: number;
}

export interface BudgetInfo {
  current_budget: number;
  initial_budget: number;
  spent: number;
  source: string | null;
}

export interface AgentSummary {
  id: string;
  role: AgentRole;
  status: AgentStatus;
  task_description: string;

  // Complexity evaluation
  complexity: string | null;
  complexity_reasoning: string | null;

  // For WORKER agents
  worker_tool: string | null;

  // For MANAGER agents
  subtasks: SubtaskSummary[];

  // Configuration
  config_strategy: string | null;
  config_details: Record<string, unknown>;

  // Result/Error
  result: string | null;
  error_message: string | null;

  // Budget information
  budget: BudgetInfo | null;

  // Task queue
  task_queue: TaskQueueItem[];
  queue_size: number;
}

// System Configuration types

export interface InfrastructureConfig {
  llm_model_boss: string;
  worker_tool_type: string;
  worker_tool_model: string;
  worker_tool_timeout: number;
}

export interface ApplicationConfig {
  max_retries: number;
  retry_delay: number;
  poll_interval: number;
  llm_timeout: number;
  worker_timeout: number;
  default_task_complexity_threshold: number;
}

export interface SystemConfig {
  infrastructure: InfrastructureConfig;
  application: ApplicationConfig;
}
