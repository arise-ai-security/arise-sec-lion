"""Tests for E.4 Part 2 (step 4.2) — Docker PID-label cleanup handler.

The handler is registered with the :class:`CleanupRegistry` at
``create_runtime_cli`` time when the active domain plugin is
:class:`SecurityDomainPlugin`. When invoked, it lists every container
labeled with the current process's PID and force-removes them via
``docker rm -f``.

These tests verify both the registration and the subprocess invocations
without spinning up real Docker.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = REPO_ROOT / "config" / "config.yaml"


@pytest.fixture(autouse=True)
def _postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")


def _settings_with(tmp_path: Path, **overrides: Any) -> Path:
    """Write a YAML overlay on ``config.yaml`` and return its path."""
    payload = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
    payload.setdefault("boss", {})["model"] = "test-boss-model"
    payload.setdefault("manager", {})["model"] = "test-manager-model"
    payload.setdefault("worker", {})["model"] = "test-worker-model"
    for top_key, sub in overrides.items():
        existing = payload.get(top_key, {})
        if isinstance(existing, dict) and isinstance(sub, dict):
            existing.update(sub)
            payload[top_key] = existing
        else:
            payload[top_key] = sub
    target = tmp_path / "settings.yaml"
    target.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return target


# --- handler-in-isolation behaviour --------------------------------------


def test_docker_pid_cleanup_runs_docker_rm_for_each_listed_id() -> None:
    """The handler issues ``docker rm -f`` for every container ID listed."""

    # Given: docker ps returns two container IDs for this process's PID.
    from infrastructure.cleanup.docker_pid import docker_pid_cleanup

    captured_calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        captured_calls.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            return MagicMock(stdout="abc123\ndef456\n", stderr="", returncode=0)
        return MagicMock(stdout="", stderr="", returncode=0)

    # When: the handler runs.
    with (
        patch("infrastructure.cleanup.docker_pid.shutil.which", return_value="docker"),
        patch("infrastructure.cleanup.docker_pid.subprocess.run", side_effect=_fake_run),
    ):
        docker_pid_cleanup()

    # Then: exactly two subprocess invocations — a listing then a removal.
    assert len(captured_calls) == 2

    pid = os.getpid()
    listing_argv = captured_calls[0]
    assert listing_argv[:2] == ["docker", "ps"]
    assert "-a" in listing_argv
    assert "--filter" in listing_argv
    assert f"label=arise.session_pid={pid}" in listing_argv
    assert "--format" in listing_argv
    assert "{{.ID}}" in listing_argv

    # And: the removal targets exactly the IDs returned by the listing.
    removal_argv = captured_calls[1]
    assert removal_argv == ["docker", "rm", "-f", "abc123", "def456"]


def test_docker_pid_cleanup_noop_when_no_containers_match() -> None:
    """The handler does NOT invoke ``docker rm`` when no IDs are listed."""

    # Given: docker ps returns an empty body.
    from infrastructure.cleanup.docker_pid import docker_pid_cleanup

    captured_calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        captured_calls.append(list(argv))
        return MagicMock(stdout="", stderr="", returncode=0)

    # When: the handler runs.
    with (
        patch("infrastructure.cleanup.docker_pid.shutil.which", return_value="docker"),
        patch("infrastructure.cleanup.docker_pid.subprocess.run", side_effect=_fake_run),
    ):
        docker_pid_cleanup()

    # Then: only the listing call was made; no removal call.
    assert len(captured_calls) == 1
    assert captured_calls[0][:2] == ["docker", "ps"]


def test_docker_pid_cleanup_swallows_subprocess_errors() -> None:
    """The handler logs and swallows any subprocess failure."""

    # Given: subprocess.run raises a generic error on the listing call.
    from infrastructure.cleanup.docker_pid import docker_pid_cleanup

    def _boom(argv, **kwargs):
        del argv, kwargs
        raise RuntimeError("docker daemon unreachable")

    # When/Then: the handler does NOT propagate the exception.
    with patch("infrastructure.cleanup.docker_pid.subprocess.run", side_effect=_boom):
        docker_pid_cleanup()  # must not raise


def test_docker_pid_cleanup_swallows_check_failures_on_listing() -> None:
    """A non-zero ``docker ps`` exit is swallowed (logged and ignored)."""

    # Given: subprocess.run raises CalledProcessError on the listing call.
    import subprocess

    from infrastructure.cleanup.docker_pid import docker_pid_cleanup

    def _fail(argv, **kwargs):
        del kwargs
        raise subprocess.CalledProcessError(returncode=1, cmd=argv)

    # When/Then: handler completes silently — best-effort contract.
    with patch("infrastructure.cleanup.docker_pid.subprocess.run", side_effect=_fail):
        docker_pid_cleanup()


# --- registration via create_runtime_cli ---------------------------------


def test_create_runtime_cli_registers_docker_pid_handler_when_security_enabled(
    tmp_path: Path,
) -> None:
    """``create_runtime_cli`` registers ``docker-by-pid`` when the security plugin is active."""

    # Given: a settings overlay with security.enabled=true (the default in
    #        config.yaml) and the security domain components wired through.
    from bootstrap.composition import (
        _build_security_components,
        create_runtime_cli,
    )
    from config.settings import Settings
    from infrastructure.cleanup.registry import CleanupRegistry

    settings_path = _settings_with(tmp_path)
    settings = Settings.from_yaml(settings_path)

    registry = CleanupRegistry()
    domain_components = _build_security_components(settings)

    # When: building the runtime CLI with the registry threaded through.
    create_runtime_cli(
        settings,
        domain_components=domain_components,
        cleanup_registry=registry,
    )

    # Then: the handler is registered under the documented name and is
    #       a callable referencing _docker_pid_cleanup.
    assert "docker-by-pid" in registry._handlers
    handler = registry._handlers["docker-by-pid"]
    assert callable(handler)


def test_create_runtime_cli_skips_registration_when_no_domain_plugin(
    tmp_path: Path,
) -> None:
    """No domain plugin -> no ``docker-by-pid`` handler is registered."""

    # Given: a settings overlay with security disabled.
    from bootstrap.composition import create_runtime_cli
    from config.settings import Settings
    from infrastructure.cleanup.registry import CleanupRegistry

    settings_path = _settings_with(tmp_path, security={"enabled": False})
    settings = Settings.from_yaml(settings_path)

    registry = CleanupRegistry()

    # When: building without domain components.
    create_runtime_cli(settings, cleanup_registry=registry)

    # Then: the registry remains empty.
    assert "docker-by-pid" not in registry._handlers


def test_create_runtime_cli_skips_registration_when_registry_is_none(
    tmp_path: Path,
) -> None:
    """``cleanup_registry=None`` skips registration even with security enabled."""

    # Given: a security-enabled settings overlay but no registry.
    from bootstrap.composition import (
        _build_security_components,
        create_runtime_cli,
    )
    from config.settings import Settings

    settings_path = _settings_with(tmp_path)
    settings = Settings.from_yaml(settings_path)
    domain_components = _build_security_components(settings)

    # When/Then: building succeeds; nothing to verify but absence of error.
    create_runtime_cli(
        settings,
        domain_components=domain_components,
        cleanup_registry=None,
    )


def test_registered_handler_invokes_docker_rm_via_registry(tmp_path: Path) -> None:
    """End-to-end: registry sweep triggers ``docker rm`` for matching IDs."""

    # Given: a registry+handler wired through create_runtime_cli.
    from bootstrap.composition import (
        _build_security_components,
        create_runtime_cli,
    )
    from config.settings import Settings
    from infrastructure.cleanup.registry import CleanupRegistry

    settings_path = _settings_with(tmp_path)
    settings = Settings.from_yaml(settings_path)
    domain_components = _build_security_components(settings)

    registry = CleanupRegistry()
    create_runtime_cli(
        settings,
        domain_components=domain_components,
        cleanup_registry=registry,
    )

    captured_calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        captured_calls.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            return MagicMock(stdout="container-pid-leak\n", stderr="", returncode=0)
        return MagicMock(stdout="", stderr="", returncode=0)

    # When: the registry sweep fires.
    with (
        patch("infrastructure.cleanup.docker_pid.shutil.which", return_value="docker"),
        patch("infrastructure.cleanup.docker_pid.subprocess.run", side_effect=_fake_run),
    ):
        registry.run_all()

    # Then: the registered handler reached docker rm -f <id>.
    assert len(captured_calls) == 2
    assert captured_calls[0][:2] == ["docker", "ps"]
    assert captured_calls[1] == ["docker", "rm", "-f", "container-pid-leak"]
