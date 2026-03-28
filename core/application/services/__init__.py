"""Application services for agent execution orchestration.

Re-exports from subpackages for backward compatibility.
New code should import directly from the subpackage:
    from core.application.services.lifecycle.agent_repository import AgentRepository
"""

# --- lifecycle ---
from core.application.services.lifecycle.agent_repository import (
    AgentNotFoundError,
    AgentRepository,
)
from core.application.services.lifecycle.child_factory import ChildAgentFactory
from core.application.services.lifecycle.hierarchy_limits_registry import (
    HierarchyLimitsRegistry,
)
from core.application.services.lifecycle.parent_notifier import (
    ParentNotificationService,
)
from core.application.services.lifecycle.role_dispatch import (
    DispatchContext,
    EvaluatorHandler,
    LifecycleRoleHandler,
    PendingHandler,
    RoleHandler,
    WorkerHandler,
    build_role_handlers,
)

# --- orchestration ---
from core.application.services.orchestration.context_condenser import (
    ContextCondenser,
)
from core.application.services.orchestration.llm_query_executor import (
    LLMQueryExecutor,
)
from core.application.services.orchestration.retry_policy import RetryPolicy
from core.application.services.orchestration.verification_pipeline import (
    VerificationPipeline,
)

# --- prompt ---
from core.application.services.prompt.prompt_builder import PromptBuilder, TemplateChain
from core.application.services.prompt.prompt_parser import PromptParser
from core.application.services.prompt.prompt_strategy import (
    PromptContext,
    PromptStrategy,
    SubtaskScope,
)
from core.application.services.prompt.prompt_trace_service import PromptTraceService

# --- query ---
from core.application.services.query.event_broadcaster import EventBroadcaster
from core.application.services.query.query_service import (
    AgentQueryService,
    AgentSummaryReadModel,
)

# --- toolset ---
from core.application.services.toolset.tool_calling_service import (
    ToolCallingService,
    ToolRecord,
)
from core.application.services.toolset.toolset_context import (
    ActiveToolContext,
    LoopPolicy,
    ToolsetPolicy,
    build_prompt_capabilities,
)
from core.application.services.toolset.toolset_policy_resolver import (
    ToolsetPolicyResolver,
)


__all__ = [
    "ActiveToolContext",
    "AgentNotFoundError",
    "AgentQueryService",
    "AgentRepository",
    "AgentSummaryReadModel",
    "ChildAgentFactory",
    "ContextCondenser",
    "DispatchContext",
    "EvaluatorHandler",
    "EventBroadcaster",
    "HierarchyLimitsRegistry",
    "LLMQueryExecutor",
    "LifecycleRoleHandler",
    "LoopPolicy",
    "ParentNotificationService",
    "PendingHandler",
    "PromptBuilder",
    "PromptContext",
    "PromptParser",
    "PromptStrategy",
    "PromptTraceService",
    "RetryPolicy",
    "RoleHandler",
    "SubtaskScope",
    "TemplateChain",
    "ToolCallingService",
    "ToolRecord",
    "ToolsetPolicy",
    "ToolsetPolicyResolver",
    "VerificationPipeline",
    "WorkerHandler",
    "build_prompt_capabilities",
    "build_role_handlers",
]
