"""Tests for ConfigResolver domain service.

This module tests the heuristic strategy implementation for resolving
operation-specific LLM configurations from high-level agent configs.

Simplified to heuristic strategy only.
"""

import pytest

from core.domain.values.agent_config import HeuristicConfig, LLMConfig
from core.domain.services.config_resolver import ConfigResolver


class TestConfigResolverHeuristic:
    """Test ConfigResolver with heuristic strategy."""

    def test_resolve_complexity_evaluation_applies_heuristic(self):
        """Test that heuristic strategy lowers temperature for complexity eval."""
        config = HeuristicConfig(
            base=LLMConfig(
                model="gpt-4o",
                temperature=0.7,
                max_tokens=1000,
            ),
            tool="claude_code",
        )

        resolved = ConfigResolver.resolve(config, operation="complexity_evaluation")

        # Heuristic: temperature lowered to 0.3
        assert resolved.model == "gpt-4o"
        assert resolved.temperature == 0.3
        # Heuristic: max_tokens capped at 500
        assert resolved.max_tokens == 500

    def test_resolve_complexity_evaluation_caps_tokens(self):
        """Test that heuristic caps max_tokens at 500 for complexity eval."""
        config = HeuristicConfig(
            base=LLMConfig(
                model="gpt-4o",
                temperature=0.7,
                max_tokens=2000,  # Higher than cap
            ),
            tool="claude_code",
        )

        resolved = ConfigResolver.resolve(config, operation="complexity_evaluation")

        # Should cap at 500
        assert resolved.max_tokens == 500

    def test_resolve_complexity_evaluation_preserves_low_tokens(self):
        """Test that heuristic preserves max_tokens if already below cap."""
        config = HeuristicConfig(
            base=LLMConfig(
                model="gpt-4o",
                temperature=0.7,
                max_tokens=300,  # Already below cap
            ),
            tool="claude_code",
        )

        resolved = ConfigResolver.resolve(config, operation="complexity_evaluation")

        # Should preserve 300
        assert resolved.max_tokens == 300

    def test_resolve_task_decomposition_uses_base(self):
        """Test that task decomposition uses base config as-is."""
        config = HeuristicConfig(
            base=LLMConfig(
                model="gemini-pro",
                temperature=0.8,
                max_tokens=1500,
            ),
            tool="claude_code",
        )

        resolved = ConfigResolver.resolve(config, operation="task_decomposition")

        # Should use base config unchanged
        assert resolved.model == "gemini-pro"
        assert resolved.temperature == 0.8
        assert resolved.max_tokens == 1500


class TestConfigResolverEdgeCases:
    """Test ConfigResolver edge cases and error handling."""

    def test_unknown_operation_raises_error(self):
        """Test that unknown operation raises ValueError."""
        config = HeuristicConfig(
            base=LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
            tool="claude_code",
        )

        with pytest.raises(ValueError, match="Unknown operation"):
            ConfigResolver.resolve(config, operation="unknown_operation")  # type: ignore
