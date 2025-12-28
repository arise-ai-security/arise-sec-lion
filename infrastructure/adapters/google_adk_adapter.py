"""Google ADK Adapter - re-exports from worker package for backward compatibility.

DEPRECATED: Import from infrastructure.adapters.worker instead.
"""

from infrastructure.adapters.worker.google_adk_adapter import (
    ADKAdapterConfig,
    GoogleADKAdapter,
    execute_command,
)
from infrastructure.adapters.worker.shared.cost_calculator import get_model_pricing

# Backward compatibility: provide pricing constants from registry
_gemini_pricing = get_model_pricing("gemini-3-pro")
GEMINI_3_PRO_INPUT_PRICE_PER_M = _gemini_pricing.input_per_million
GEMINI_3_PRO_OUTPUT_PRICE_PER_M = _gemini_pricing.output_per_million

__all__ = [
    "GoogleADKAdapter",
    "ADKAdapterConfig",
    "execute_command",
    "GEMINI_3_PRO_INPUT_PRICE_PER_M",
    "GEMINI_3_PRO_OUTPUT_PRICE_PER_M",
]
