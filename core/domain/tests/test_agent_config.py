"""Tests for agent configuration Pydantic models.

This module tests the type-safe configuration models that enable
parent agents to control child configurations dynamically.

Simplified to heuristic strategy only.
"""

import pytest
from pydantic import ValidationError

from core.domain.values.agent_config import HeuristicConfig, LLMConfig


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

    def test_tool_validation(self):
        """Test that tool must be valid literal."""
        # Valid tools
        for tool in ["claude_code", "openhands", "google_adk"]:
            config = HeuristicConfig(
                base=LLMConfig(model="gpt-4o", temperature=0.5, max_tokens=500),
                tool=tool,  # type: ignore
            )
            assert config.tool == tool

        # Invalid tool
        with pytest.raises(ValidationError):
            HeuristicConfig(
                base=LLMConfig(model="gpt-4o", temperature=0.5, max_tokens=500),
                tool="invalid_tool",  # type: ignore
            )

    def test_immutability(self):
        """Test that HeuristicConfig is immutable (frozen)."""
        config = HeuristicConfig(
            base=LLMConfig(model="gpt-4o", temperature=0.5, max_tokens=500)
        )
        with pytest.raises(ValidationError):
            config.tool = "openhands"  # type: ignore

    def test_from_dict(self):
        """Test creating HeuristicConfig from dict (as parser would)."""
        config_dict = {
            "strategy": "heuristic",
            "base": {"model": "gpt-4o", "temperature": 0.5, "max_tokens": 500},
            "tool": "claude_code",
        }
        config = HeuristicConfig(**config_dict)
        assert isinstance(config, HeuristicConfig)
        assert config.base.model == "gpt-4o"
