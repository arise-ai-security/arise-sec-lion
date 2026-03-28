"""Worker adapters for task execution.

Provides adapters for executing tasks via various AI coding assistants:
- ClaudeAgentSDKAdapter: Claude Code via official SDK
- OpenHandsAdapter: OpenHands AI developer
- GoogleADKAdapter: Google ADK with Gemini models
"""

from .base import WorkerAdapterBase
from .claude_sdk_adapter import ClaudeAgentSDKAdapter, SDKAdapterConfig
from .google_adk_adapter import ADKAdapterConfig, GoogleADKAdapter, execute_command
from .openhands_adapter import OpenHandsAdapter
from .shared import (
    TOOL_FORMATTERS,
    EventSequencer,
    ModelPricing,
    format_tool_event,
    get_model_pricing,
    validate_task_context,
)


__all__ = [
    # Base class
    "WorkerAdapterBase",
    # Claude SDK adapter
    "ClaudeAgentSDKAdapter",
    "SDKAdapterConfig",
    # OpenHands adapter
    "OpenHandsAdapter",
    # Google ADK adapter
    "GoogleADKAdapter",
    "ADKAdapterConfig",
    "execute_command",
    # Shared utilities
    "EventSequencer",
    "TOOL_FORMATTERS",
    "format_tool_event",
    "validate_task_context",
    "ModelPricing",
    "get_model_pricing",
]
