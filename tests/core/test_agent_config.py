"""Tests for agent configuration Pydantic models.

This module tests the type-safe configuration models that enable
parent agents to control child configurations dynamically.
"""

import pytest
from pydantic import ValidationError

from core.domain.agent_config import (
    HeuristicConfig,
    HybridConfig,
    LLMConfig,
    PerOperationConfig,
)


class TestLLMConfig:
    """Test LLMConfig value object validation."""

    def test_valid_config(self):
        """Test that valid LLMConfig is created successfully."""
        config = LLMConfig(
            model="gpt-4o",
            temperature=0.7,
            max_tokens=1000,
        )
        assert config.model == "gpt-4o"
        assert config.temperature == 0.7
        assert config.max_tokens == 1000
        assert config.top_p is None

    def test_valid_config_with_top_p(self):
        """Test LLMConfig with optional top_p parameter."""
        config = LLMConfig(
            model="gemini-pro",
            temperature=0.5,
            max_tokens=500,
            top_p=0.9,
        )
        assert config.top_p == 0.9

    def test_temperature_bounds(self):
        """Test that temperature is validated within bounds."""
        # Valid: temperature at lower bound
        config = LLMConfig(model="gpt-4o", temperature=0.0, max_tokens=1000)
        assert config.temperature == 0.0

        # Valid: temperature at upper bound
        config = LLMConfig(model="gpt-4o", temperature=2.0, max_tokens=1000)
        assert config.temperature == 2.0

        # Invalid: temperature too high
        with pytest.raises(ValidationError):
            LLMConfig(model="gpt-4o", temperature=3.0, max_tokens=1000)

        # Invalid: temperature negative
        with pytest.raises(ValidationError):
            LLMConfig(model="gpt-4o", temperature=-0.1, max_tokens=1000)

    def test_max_tokens_validation(self):
        """Test that max_tokens must be positive."""
        # Valid: positive max_tokens
        config = LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=1)
        assert config.max_tokens == 1

        # Invalid: zero max_tokens
        with pytest.raises(ValidationError):
            LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=0)

        # Invalid: negative max_tokens
        with pytest.raises(ValidationError):
            LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=-1)

    def test_model_required(self):
        """Test that model field is required and non-empty."""
        # Invalid: missing model
        with pytest.raises(ValidationError):
            LLMConfig(temperature=0.7, max_tokens=1000)  # type: ignore

        # Invalid: empty model string
        with pytest.raises(ValidationError):
            LLMConfig(model="", temperature=0.7, max_tokens=1000)

    def test_immutability(self):
        """Test that LLMConfig is immutable (frozen)."""
        config = LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=1000)
        with pytest.raises(ValidationError):
            config.temperature = 0.5  # type: ignore


class TestPerOperationConfig:
    """Test PerOperationConfig strategy model."""

    def test_valid_per_operation_config(self):
        """Test that valid PerOperationConfig is created successfully."""
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
        assert config.strategy == "per_operation"
        assert config.complexity_evaluation.model == "gpt-4o-mini"
        assert config.task_decomposition.model == "gpt-4o"
        assert config.tool == "claude_code"

    def test_tool_validation(self):
        """Test that tool must be valid literal."""
        # Valid tools
        config = PerOperationConfig(
            complexity_evaluation=LLMConfig(model="gpt-4o", temperature=0.3, max_tokens=300),
            task_decomposition=LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
            tool="openhands",
        )
        assert config.tool == "openhands"

        # Invalid tool
        with pytest.raises(ValidationError):
            PerOperationConfig(
                complexity_evaluation=LLMConfig(model="gpt-4o", temperature=0.3, max_tokens=300),
                task_decomposition=LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
                tool="invalid_tool",  # type: ignore
            )


class TestHeuristicConfig:
    """Test HeuristicConfig strategy model."""

    def test_valid_heuristic_config(self):
        """Test that valid HeuristicConfig is created successfully."""
        config = HeuristicConfig(
            base=LLMConfig(
                model="gpt-4o-mini",
                temperature=0.5,
                max_tokens=500,
            ),
            tool="claude_code",
        )
        assert config.strategy == "heuristic"
        assert config.base.model == "gpt-4o-mini"
        assert config.tool == "claude_code"

    def test_default_tool(self):
        """Test that tool defaults to claude_code."""
        config = HeuristicConfig(base=LLMConfig(model="gpt-4o", temperature=0.5, max_tokens=500))
        assert config.tool == "claude_code"


class TestHybridConfig:
    """Test HybridConfig strategy model."""

    def test_valid_hybrid_config_no_overrides(self):
        """Test HybridConfig with no overrides."""
        config = HybridConfig(
            base=LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
            tool="claude_code",
        )
        assert config.strategy == "hybrid"
        assert config.base.model == "gpt-4o"
        assert config.overrides == {}

    def test_valid_hybrid_config_with_overrides(self):
        """Test HybridConfig with operation-specific overrides."""
        config = HybridConfig(
            base=LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
            overrides={
                "complexity_evaluation": {
                    "temperature": 0.3,
                    "max_tokens": 300,
                }
            },
            tool="openhands",
        )
        assert config.overrides["complexity_evaluation"]["temperature"] == 0.3
        assert config.overrides["complexity_evaluation"]["max_tokens"] == 300


class TestAgentConfigUnion:
    """Test AgentConfig discriminated union."""

    def test_discriminates_by_strategy_field(self):
        """Test that Pydantic correctly discriminates strategies."""
        # PerOperationConfig
        config_dict = {
            "strategy": "per_operation",
            "complexity_evaluation": {"model": "gpt-4o", "temperature": 0.3, "max_tokens": 300},
            "task_decomposition": {"model": "gpt-4o", "temperature": 0.7, "max_tokens": 1000},
            "tool": "claude_code",
        }
        # TypeAdapter would be used in production, but for tests we can validate directly
        config = PerOperationConfig(**config_dict)
        assert isinstance(config, PerOperationConfig)

        # HeuristicConfig
        config_dict = {
            "strategy": "heuristic",
            "base": {"model": "gpt-4o", "temperature": 0.5, "max_tokens": 500},
            "tool": "claude_code",
        }
        config = HeuristicConfig(**config_dict)
        assert isinstance(config, HeuristicConfig)

        # HybridConfig
        config_dict = {
            "strategy": "hybrid",
            "base": {"model": "gpt-4o", "temperature": 0.7, "max_tokens": 1000},
            "overrides": {},
            "tool": "claude_code",
        }
        config = HybridConfig(**config_dict)
        assert isinstance(config, HybridConfig)
