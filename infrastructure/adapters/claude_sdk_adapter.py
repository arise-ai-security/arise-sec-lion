"""Claude SDK Adapter - re-exports from worker package for backward compatibility.

DEPRECATED: Import from infrastructure.adapters.worker instead.
"""

from typing import Any

from infrastructure.adapters.worker.claude_sdk_adapter import (
    ClaudeAgentSDKAdapter,
    SDKAdapterConfig,
)


# Re-export private functions for test compatibility
def _process_block(block: Any) -> tuple[str, str] | None:
    """Process a single content block, returning (content, output_type) or None."""
    return ClaudeAgentSDKAdapter._process_block(block)


def _format_error(error: Exception) -> str:
    """Format exception as user-friendly error message."""
    adapter = ClaudeAgentSDKAdapter()
    return adapter._format_error(error)


__all__ = ["ClaudeAgentSDKAdapter", "SDKAdapterConfig", "_format_error", "_process_block"]
