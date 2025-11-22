"""Tests for ConfigResolver domain service.

This module tests the strategy pattern implementation for resolving
operation-specific LLM configurations from high-level agent configs.
"""

import pytest

from core.domain.agent_config import HeuristicConfig, HybridConfig, LLMConfig, PerOperationConfig
from core.domain.config_resolver import ConfigResolver


class TestConfigResolverPerOperation:
    """Test ConfigResolver with per_operation strategy."""

    def test_resolve_complexity_evaluation(self):
        """Test resolving config for complexity evaluation operation."""
        config = PerOperationConfig(
            complexity_evaluation=LLMConfig(
                model="gpt-4o-mini",
                temperature=0.3,
                max_tokens=300,
            ),
            task_decomposition=LLMConfig(
                model="gpt-4o",
                temperature=0.7,
                max_tokens=1500,
            ),
            tool="claude_code",
        )

        resolved = ConfigResolver.resolve(config, operation="complexity_evaluation")

        assert resolved.model == "gpt-4o-mini"
        assert resolved.temperature == 0.3
        assert resolved.max_tokens == 300

    def test_resolve_task_decomposition(self):
        """Test resolving config for task decomposition operation."""
        config = PerOperationConfig(
            complexity_evaluation=LLMConfig(
                model="gpt-4o-mini",
                temperature=0.3,
                max_tokens=300,
            ),
            task_decomposition=LLMConfig(
                model="gpt-4o",
                temperature=0.7,
                max_tokens=1500,
            ),
            tool="openhands",
        )

        resolved = ConfigResolver.resolve(config, operation="task_decomposition")

        assert resolved.model == "gpt-4o"
        assert resolved.temperature == 0.7
        assert resolved.max_tokens == 1500


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

    def test_resolve_preserves_top_p(self):
        """Test that heuristic preserves top_p if set."""
        config = HeuristicConfig(
            base=LLMConfig(
                model="gpt-4o",
                temperature=0.7,
                max_tokens=1000,
                top_p=0.9,
            ),
            tool="claude_code",
        )

        resolved = ConfigResolver.resolve(config, operation="complexity_evaluation")

        assert resolved.top_p == 0.9


class TestConfigResolverHybrid:
    """Test ConfigResolver with hybrid strategy."""

    def test_resolve_with_no_overrides(self):
        """Test hybrid strategy with no overrides uses base config."""
        config = HybridConfig(
            base=LLMConfig(
                model="gpt-4o",
                temperature=0.7,
                max_tokens=1000,
            ),
            overrides={},
            tool="claude_code",
        )

        resolved = ConfigResolver.resolve(config, operation="complexity_evaluation")

        # Should use base config unchanged
        assert resolved.model == "gpt-4o"
        assert resolved.temperature == 0.7
        assert resolved.max_tokens == 1000

    def test_resolve_with_temperature_override(self):
        """Test hybrid strategy applies temperature override."""
        config = HybridConfig(
            base=LLMConfig(
                model="gpt-4o",
                temperature=0.7,
                max_tokens=1000,
            ),
            overrides={
                "complexity_evaluation": {
                    "temperature": 0.3,
                }
            },
            tool="claude_code",
        )

        resolved = ConfigResolver.resolve(config, operation="complexity_evaluation")

        # Override applied
        assert resolved.temperature == 0.3
        # Other fields preserved from base
        assert resolved.model == "gpt-4o"
        assert resolved.max_tokens == 1000

    def test_resolve_with_multiple_overrides(self):
        """Test hybrid strategy applies multiple overrides."""
        config = HybridConfig(
            base=LLMConfig(
                model="gpt-4o",
                temperature=0.7,
                max_tokens=1000,
            ),
            overrides={
                "complexity_evaluation": {
                    "temperature": 0.3,
                    "max_tokens": 300,
                }
            },
            tool="claude_code",
        )

        resolved = ConfigResolver.resolve(config, operation="complexity_evaluation")

        # Overrides applied
        assert resolved.temperature == 0.3
        assert resolved.max_tokens == 300
        # Base field preserved
        assert resolved.model == "gpt-4o"

    def test_resolve_operation_without_override(self):
        """Test hybrid resolves operation without override using base."""
        config = HybridConfig(
            base=LLMConfig(
                model="gpt-4o",
                temperature=0.7,
                max_tokens=1000,
            ),
            overrides={
                "complexity_evaluation": {
                    "temperature": 0.3,
                }
            },
            tool="claude_code",
        )

        # Resolve operation that has no override
        resolved = ConfigResolver.resolve(config, operation="task_decomposition")

        # Should use base config unchanged
        assert resolved.model == "gpt-4o"
        assert resolved.temperature == 0.7
        assert resolved.max_tokens == 1000

    def test_resolve_validates_merged_config(self):
        """Test that hybrid strategy validates merged config."""
        config = HybridConfig(
            base=LLMConfig(
                model="gpt-4o",
                temperature=0.7,
                max_tokens=1000,
            ),
            overrides={
                "complexity_evaluation": {
                    "temperature": 5.0,  # Invalid: too high
                }
            },
            tool="claude_code",
        )

        # Should raise ValueError due to Pydantic validation
        with pytest.raises(ValueError, match="Invalid config"):
            ConfigResolver.resolve(config, operation="complexity_evaluation")


class TestConfigResolverEdgeCases:
    """Test ConfigResolver edge cases and error handling."""

    def test_unknown_operation_per_operation(self):
        """Test that unknown operation raises ValueError with per_operation."""
        config = PerOperationConfig(
            complexity_evaluation=LLMConfig(model="gpt-4o", temperature=0.3, max_tokens=300),
            task_decomposition=LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
            tool="claude_code",
        )

        with pytest.raises(ValueError, match="not configured"):
            ConfigResolver.resolve(config, operation="unknown_operation")  # type: ignore

    def test_unknown_operation_heuristic(self):
        """Test that unknown operation raises ValueError with heuristic."""
        config = HeuristicConfig(
            base=LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
            tool="claude_code",
        )

        with pytest.raises(ValueError, match="Unknown operation"):
            ConfigResolver.resolve(config, operation="unknown_operation")  # type: ignore
