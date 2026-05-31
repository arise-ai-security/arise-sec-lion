"""Tests for the in-container Claude env allowlist."""

from __future__ import annotations

import pytest

from infrastructure.adapters.worker.shared.container_exec import (
    CONTAINER_ENV_ALLOWLIST,
    build_claude_container_env,
)


def test_prompt_caching_1h_is_in_allowlist() -> None:
    # Given/Then: the 1-hour cache flag is forwardable into containers
    assert "ENABLE_PROMPT_CACHING_1H" in CONTAINER_ENV_ALLOWLIST


def test_build_claude_container_env_forwards_prompt_caching_1h_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the host opts into the 1-hour prompt-cache TTL
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-123")
    monkeypatch.setenv("ENABLE_PROMPT_CACHING_1H", "1")

    # When: building the in-container env
    env = build_claude_container_env(scratch_container_dir=None)

    # Then: the flag is forwarded into the container alongside the API key
    assert env["ENABLE_PROMPT_CACHING_1H"] == "1"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-123"


def test_build_claude_container_env_omits_prompt_caching_1h_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the host does not set the 1-hour cache flag
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-123")
    monkeypatch.delenv("ENABLE_PROMPT_CACHING_1H", raising=False)

    # When: building the in-container env
    env = build_claude_container_env(scratch_container_dir=None)

    # Then: the flag is absent (forwarded only when set, matching host path)
    assert "ENABLE_PROMPT_CACHING_1H" not in env
