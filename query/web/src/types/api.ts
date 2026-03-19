/**
 * TypeScript types matching the FastAPI Pydantic schemas.
 * These mirror the definitions in presentation/api/schemas.py
 */

// Agent types
export type AgentRole = 'BOSS' | 'MANAGER' | 'WORKER' | 'PENDING';
export type AgentStatus = 'pending' | 'analyzing' | 'in_progress' | 'waiting' | 'completed' | 'failed' | 'blocked';

export interface AgentListItem {
  id: string;
  role: AgentRole;
  status: AgentStatus;
  task_description: string;
  created_at: string | null;
  instance_id: string | null;
}

export interface PaginationMeta {
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
}

export interface PaginatedAgentList {
  items: AgentListItem[];
  pagination: PaginationMeta;
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
  poll_interval: number;
}

export interface SystemConfig {
  infrastructure: InfrastructureConfig;
  application: ApplicationConfig;
}

// =============================================================================
// Execution Summary Types (Cost, Timing, Node Counts)
// =============================================================================

/** Cost breakdown by agent role. */
export interface RoleCostBreakdown {
  BOSS: number;
  MANAGER: number;
  WORKER: number;
  PENDING: number;
  UNKNOWN: number;
}

/** Agent counts by role. */
export interface RoleCount {
  BOSS: number;
  MANAGER: number;
  WORKER: number;
  PENDING: number;
  total: number;
}

/** Token usage by role. */
export interface RoleTokens {
  BOSS: number;
  MANAGER: number;
  WORKER: number;
  PENDING: number;
}

/** Execution timing breakdown. */
export interface ExecutionTiming {
  /** Total execution time in seconds. */
  total_seconds: number;
  /** Execution time by role (role -> seconds). */
  by_role: Record<string, number>;
  /** Time spent in each phase (phase -> seconds). */
  by_phase: Record<string, number>;
  /** Execution time per agent (agent_id -> seconds). */
  by_agent: Record<string, number>;
}

/** Comprehensive cost breakdown. */
export interface CostBreakdown {
  /** Total cost in USD. */
  total_cost_usd: number;
  /** Cost from LLM calls. */
  llm_cost_usd: number;
  /** Cost from worker tool execution. */
  worker_cost_usd: number;

  /** Total tokens consumed. */
  total_tokens: number;
  /** Input tokens consumed. */
  prompt_tokens: number;
  /** Output tokens consumed. */
  completion_tokens: number;

  /** Cost breakdown by agent role. */
  cost_by_role: RoleCostBreakdown;
  /** Cost breakdown by LLM model. */
  cost_by_model: Record<string, number>;
  /** Cost breakdown by operation type. */
  cost_by_operation: Record<string, number>;
  /** Cost breakdown by agent ID. */
  cost_by_agent: Record<string, number>;
  /** Token usage by role. */
  tokens_by_role: RoleTokens;

  /** Budget limit if configured. */
  budget_limit_usd: number | null;
  /** Remaining budget. */
  budget_remaining_usd: number | null;
  /** Whether budget was exceeded. */
  budget_exceeded: boolean;
}

/** Comprehensive execution summary for an agent hierarchy. */
export interface ExecutionSummary {
  /** Total number of domain events. */
  total_events: number;
  /** Event counts by type name. */
  events_by_type: Record<string, number>;
  /** Timestamp of first event. */
  first_event: string | null;
  /** Timestamp of last event. */
  last_event: string | null;
  /** Number of WorkFailed events. */
  error_count: number;

  /** Agent counts by role. */
  node_counts: RoleCount;

  /** Cost breakdown. */
  cost: CostBreakdown;

  /** Execution timing details. */
  timing: ExecutionTiming;

  /** Whether all agents have completed. */
  is_complete: boolean;
}

// =============================================================================
// Prompt Types (for observability)
// =============================================================================

/** A prompt sent by an agent. */
export interface AgentPrompt {
  /** Full prompt text. */
  prompt: string;
  /** Type of prompt (complexity_evaluation, task_decomposition, worker_execution). */
  prompt_type: string;
  /** Where prompt was sent (llm, claude_code, openhands, etc.). */
  target: string;
  /** When the prompt was sent. */
  occurred_at: string;
}

/** List of prompts for an agent. */
export interface AgentPrompts {
  /** Agent UUID. */
  agent_id: string;
  /** List of prompts. */
  prompts: AgentPrompt[];
  /** Total number of prompts. */
  total: number;
}

// =============================================================================
// Prompt Trace Types (for hierarchy visualization)
// =============================================================================

/** Source provenance of a prompt section. */
export type SectionProvenance = 'template' | 'parent' | 'sibling' | 'children' | 'shared' | 'system';

/** A single parsed section from a prompt. */
export interface PromptSection {
  /** XML tag name (e.g., 'ROLE', 'parent-context'). */
  tag: string;
  /** Section content. */
  content: string;
  /** Source provenance type. */
  provenance: SectionProvenance;
}

/** A parsed prompt with sections grouped by provenance. */
export interface ParsedPrompt {
  /** Original raw prompt text. */
  raw: string;
  /** Length of raw prompt in characters. */
  raw_length: number;
  /** When prompt was sent. */
  occurred_at: string;
  /** Type: complexity_evaluation, task_decomposition, worker_execution. */
  prompt_type: string;
  /** Target: llm, claude_code, openhands, google_adk. */
  target: string;
  /** All parsed sections in order. */
  sections: PromptSection[];
  /** Sections grouped by provenance type. */
  sections_by_provenance: Record<SectionProvenance, PromptSection[]>;
}

/** An agent node in the trace hierarchy tree. */
export interface TraceAgentNode {
  /** Agent UUID. */
  agent_id: string;
  /** Agent role (boss, manager, worker, pending). */
  role: string;
  /** Depth in hierarchy (root=0). */
  depth: number;
  /** Task description. */
  task: string;
  /** Position among siblings (0-indexed). */
  sibling_index: number;
  /** Number of prompts sent by this agent. */
  prompt_count: number;
  /** Parsed prompts with provenance. */
  prompts: ParsedPrompt[];
  /** Child agent nodes. */
  children: TraceAgentNode[];
}

/** Complete hierarchy trace response. */
export interface HierarchyTrace {
  /** Root agent node with full tree. */
  root: TraceAgentNode;
  /** Total agents in hierarchy. */
  total_agents: number;
  /** Maximum depth reached. */
  max_depth: number;
}
