"""No-op PromptStrategy for experiments that isolate the orchestration factor.

Used in cell B1 of the tree-vs-flat experiment: the tree runs without any
SEC-bench-specific prompt injection, so core falls back to default role
templates. Container runtime + security tool injection remain active via
the plugin's other methods.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from core.application.services.prompt.prompt_builder import TemplateChain
    from core.application.services.prompt.prompt_strategy import PromptContext


class NullPromptStrategy:
    """Returns None from every extend_*_prompt, deferring to default prompts."""

    def extend_assessment_prompt(
        self,
        chain: TemplateChain,
        context: PromptContext,
    ) -> TemplateChain | None:
        return None

    def extend_boss_prompt(
        self,
        chain: TemplateChain,
        context: PromptContext,
    ) -> TemplateChain | None:
        return None

    def extend_manager_prompt(
        self,
        chain: TemplateChain,
        context: PromptContext,
    ) -> TemplateChain | None:
        return None

    def extend_worker_prompt(
        self,
        chain: TemplateChain,
        context: PromptContext,
    ) -> TemplateChain | None:
        return None
