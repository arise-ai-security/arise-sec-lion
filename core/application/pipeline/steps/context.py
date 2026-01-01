"""Context injection steps for pipeline execution.

These steps handle context composition for prompts, adding global
configuration and other context data to the ContextComposer.
"""

from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineState, StepResult
from core.application.services.context_composer import ContextComposer

if TYPE_CHECKING:
    from core.application.services.global_config_provider import GlobalConfigProvider


class InjectGlobalConfig:
    """Inject global configuration into the context composer.

    This step ensures that global configuration (e.g., current_date)
    is available in prompts for all agent types. It creates a
    ContextComposer if one doesn't exist and adds GlobalConfig to it.

    Example:
        provider = GlobalConfigProvider()
        provider.set_current_date("2025-12-31")

        step = InjectGlobalConfig(provider)
        # After execution, state.context_composer will contain GlobalConfig
    """

    def __init__(self, global_config_provider: "GlobalConfigProvider") -> None:
        """Initialize with global config provider.

        Args:
            global_config_provider: Provider for global configuration data.
        """
        self._provider = global_config_provider

    async def execute(self, state: PipelineState) -> StepResult:
        """Inject global config into context composer.

        Creates a new ContextComposer if needed and adds the global
        configuration from the provider.

        Args:
            state: Current pipeline state.

        Returns:
            StepResult with updated state containing context_composer.
        """
        # Create or reuse existing composer
        composer = state.context_composer or ContextComposer()

        # Add global config if provider has any
        if self._provider:
            composer.add(self._provider.to_global_config())

        return StepResult.ok(state.with_context_composer(composer))
