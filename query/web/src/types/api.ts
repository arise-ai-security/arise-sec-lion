/**
 * TypeScript types matching the FastAPI Pydantic schemas.
 * These mirror the definitions in presentation/api/schemas.py
 */

// Agent types
export type AgentRole = 'BOSS' | 'MANAGER' | 'WORKER' | 'PENDING';
export type AgentStatus = 'pending' | 'analyzing' | 'in_progress' | 'waiting' | 'completed' | 'failed' | 'blocked' | 'terminated' | 'verifying';

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

export interface SubtaskJustification {
  parent_task: string;
  split_reason: string;
  objective: string;
  plan: string;
  why_it_may_work: string;
  expected_results: string;
  // Budget allocation reasoning
  budget_allocation: string;
  complexity_assessment: string;
  significance_weight: string;
  resource_justification: string;
}

/** Worker's report upon completing a task. */
export interface WorkerReport {
  original_task: string;
  approach: string;
  reasoning: string;
  deliverables: string;
  challenges: string;
  // New fields for actual work observations and fulfillment evidence
  observations: string;
  fulfillment_evidence: string;
}

/** Child worker's report with agent context (for MANAGER/BOSS summary). */
export interface ChildWorkerReport {
  agent_id: string;
  task: string;
  status: string;
  report: WorkerReport | null;
}

/** Aggregated summary of all subordinates' work for supervisor/BOSS nodes. */
export interface AggregatedSummary {
  total_workers: number;
  completed_workers: number;
  failed_workers: number;
  combined_deliverables: string;
  combined_approach: string;
  key_challenges: string;
}

export interface SubtaskSummary {
  description: string;
  justification: SubtaskJustification;
  budget_weight: number;
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

// =============================================================================
// Context Dashboard Types (Cross-Session Knowledge Sharing)
// =============================================================================

/** Source context data extracted from Boss prompt. */
export interface SourceContextData {
  bug_summary: string;
  error_messages: string[];
  reproduction_steps: string;
  file_paths: string[];
  commit_references: string[];
  urls: string[];
  environment: string;
  dependencies: string[];
  key_facts: string[];
  // Security/CVE build context fields
  dockerfile: string;
  build_script: string;
  work_dir: string;
  poc_command: string;
  sanitizer: string;
  cve_id: string;
  repo_url: string;
  // CWE Pattern Inference (inferred from bug report analysis)
  inferred_cwes: string[];
  cwe_reasoning: Record<string, string>;
  cwe_confidence: Record<string, string>;
  recommended_sanitizers: string[];
  fix_patterns: Record<string, string>;
}

/** Context entry from the global context dashboard. */
export interface ContextEntry {
  entry_id: string;
  entry_type: 'worker' | 'source';
  work_title: string;
  objective: string;
  justification: string;
  work_analysis: string;
  approach: string | null;
  challenges: string | null;
  source_context: SourceContextData | null;
  created_at: string;
  tags: string[];
}

/** Context published by a supervisor to the dashboard - full key-value submission. */
export interface PublishedContext {
  entry_id: string;
  entry_type: 'worker' | 'source';
  work_title: string;
  worker_id: string;
  objective: string;
  justification: string;
  work_analysis: string;
  approach: string | null;
  challenges: string | null;
  source_context: SourceContextData | null;
  tags: string[];
  published_at: string;
}

/** Context inherited by a worker from previous sessions. */
export interface InheritedContext {
  entries: ContextEntry[];
  total_available: number;
}

export interface AgentSummary {
  id: string;
  role: AgentRole;
  status: AgentStatus;
  task_description: string;

  // Supervisor's justification (from parent agent who assigned this task)
  supervisor_justification: SubtaskJustification | null;

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

  // Worker report (for WORKER agents)
  worker_report: WorkerReport | null;

  // Child worker reports (for MANAGER/BOSS agents)
  child_worker_reports: ChildWorkerReport[];

  // Aggregated summary (for MANAGER/BOSS agents)
  aggregated_summary: AggregatedSummary | null;

  // Budget information
  budget: BudgetInfo | null;

  // Task queue
  task_queue: TaskQueueItem[];
  queue_size: number;

  // Context dashboard - published by supervisor (for BOSS/MANAGER)
  published_context: PublishedContext[];

  // Context dashboard - inherited by worker (for WORKER)
  inherited_context: InheritedContext | null;
}

// System Configuration types

export interface InfrastructureConfig {
  llm_model_boss: string;
  worker_tool_type: string;
  worker_tool_model: string;
  worker_tool_timeout: number;
  unified_model: string | null;
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
