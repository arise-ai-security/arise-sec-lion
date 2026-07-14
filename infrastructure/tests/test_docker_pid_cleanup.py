"""Tests for best-effort cleanup of containers labeled with the process PID."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

from infrastructure.cleanup.docker_pid import docker_pid_cleanup


def test_cleanup_removes_every_listed_container() -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        del kwargs
        calls.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            return MagicMock(stdout="abc123\ndef456\n", stderr="", returncode=0)
        return MagicMock(stdout="", stderr="", returncode=0)

    with (
        patch("infrastructure.cleanup.docker_pid.shutil.which", return_value="docker"),
        patch("infrastructure.cleanup.docker_pid.subprocess.run", side_effect=fake_run),
    ):
        docker_pid_cleanup()

    assert f"label=arise.session_pid={os.getpid()}" in calls[0]
    assert calls[1] == ["docker", "rm", "-f", "abc123", "def456"]


def test_cleanup_does_not_remove_when_listing_is_empty() -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        del kwargs
        calls.append(list(argv))
        return MagicMock(stdout="", stderr="", returncode=0)

    with (
        patch("infrastructure.cleanup.docker_pid.shutil.which", return_value="docker"),
        patch("infrastructure.cleanup.docker_pid.subprocess.run", side_effect=fake_run),
    ):
        docker_pid_cleanup()

    assert len(calls) == 1
    assert calls[0][:2] == ["docker", "ps"]


def test_cleanup_swallows_runtime_errors() -> None:
    with patch(
        "infrastructure.cleanup.docker_pid.subprocess.run",
        side_effect=RuntimeError("docker daemon unreachable"),
    ):
        docker_pid_cleanup()


def test_cleanup_swallows_failed_list_command() -> None:
    error = subprocess.CalledProcessError(returncode=1, cmd=["docker", "ps"])
    with patch("infrastructure.cleanup.docker_pid.subprocess.run", side_effect=error):
        docker_pid_cleanup()
