"""Configuration via Pydantic Settings with phase-specific YAML support.

Re-export shim: ``config.settings`` was a single module; it is now a package
split by config domain. Every name importable from the old module stays
importable from here, so importers need not change.
"""

from .agents import BossConfig, FormatRepairerConfig, ManagerConfig
from .cors import CorsConfig
from .database import DatabaseConfig
from .loader import (
    CONFIG_DIR,
    _inject_env_database,
    _load_yaml_hierarchy,
    _merge_dicts,
    get_environment,
)
from .orchestration import ConcurrencyConfig, OrchestrationConfig, RetryConfig, TopologyConfig
from .output import OutputConfig
from .root import ApiSettings, Settings
from .security import SecurityConfig
from .toolsets import ToolCallingConfig, ToolsetConfig, ToolsetPolicyConfig, ToolsetRoleConfig
from .worker import (
    _WORKER_TOOL_PARAM_TYPES,
    ApiWorkerConfig,
    ClaudeCodeParams,
    GoogleAdkParams,
    OpenHandsParams,
    ReasoningEffort,
    WorkerConfig,
    WorkerToolParams,
    _populate_default_tool_params,
)


__all__ = [
    "CONFIG_DIR",
    "ApiSettings",
    "ApiWorkerConfig",
    "BossConfig",
    "ClaudeCodeParams",
    "ConcurrencyConfig",
    "CorsConfig",
    "DatabaseConfig",
    "FormatRepairerConfig",
    "GoogleAdkParams",
    "ManagerConfig",
    "OpenHandsParams",
    "OrchestrationConfig",
    "OutputConfig",
    "ReasoningEffort",
    "RetryConfig",
    "SecurityConfig",
    "Settings",
    "ToolCallingConfig",
    "ToolsetConfig",
    "ToolsetPolicyConfig",
    "ToolsetRoleConfig",
    "TopologyConfig",
    "WorkerConfig",
    "WorkerToolParams",
    "get_environment",
]
