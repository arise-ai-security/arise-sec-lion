"""Tests for subprocess timeout in DockerSecBenchRuntime."""

import asyncio

import pytest

from plugins.security.docker_runtime import DockerSecBenchRuntime


@pytest.fixture
def runtime() -> DockerSecBenchRuntime:
    return DockerSecBenchRuntime()


# Given: a command that hangs longer than the timeout
# When: _run_command is called with a short timeout
# Then: it returns a non-zero exit code instead of blocking forever
@pytest.mark.asyncio
async def test_run_command_respects_timeout(runtime: DockerSecBenchRuntime) -> None:
    # Given:
    exit_code, stdout, stderr = await runtime._run_command(
        ["sleep", "60"],
        timeout=1,
    )

    # Then: command was killed, non-zero exit
    assert exit_code != 0


# Given: a command that completes quickly
# When: _run_command is called with a generous timeout
# Then: it returns normally
@pytest.mark.asyncio
async def test_run_command_returns_normally_within_timeout(
    runtime: DockerSecBenchRuntime,
) -> None:
    # Given: / When:
    exit_code, stdout, stderr = await runtime._run_command(
        ["echo", "hello"],
        timeout=10,
    )

    # Then:
    assert exit_code == 0
    assert "hello" in stdout


# Given: a command that hangs
# When: _run_best_effort is called (which uses _run_command internally)
# Then: it does not block forever — returns after timeout
@pytest.mark.asyncio
async def test_run_best_effort_does_not_hang(
    runtime: DockerSecBenchRuntime,
) -> None:
    # Given: / When: should complete within ~2s, not 60s
    before = asyncio.get_event_loop().time()
    await runtime._run_best_effort(["sleep", "60"], timeout=1)
    elapsed = asyncio.get_event_loop().time() - before

    # Then:
    assert elapsed < 5
