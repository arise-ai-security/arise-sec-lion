"""Security-plugin composition tests for the process cleanup registration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_CONFIG = REPO_ROOT / "config" / "config.yaml"


class FakeCleanupRegistry:
    """Minimal registry seam consumed by the composition root."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[], None]] = {}

    def register(self, name: str, handler: Callable[[], None]) -> None:
        self.handlers[name] = handler


@pytest.fixture(autouse=True)
def _postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")


def _settings_with(tmp_path: Path, **overrides: Any) -> Path:
    payload = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
    payload.setdefault("boss", {})["model"] = "test-boss-model"
    payload.setdefault("manager", {})["model"] = "test-manager-model"
    payload.setdefault("worker", {})["model"] = "test-worker-model"
    for top_key, value in overrides.items():
        existing = payload.get(top_key, {})
        if isinstance(existing, dict) and isinstance(value, dict):
            existing.update(value)
            payload[top_key] = existing
        else:
            payload[top_key] = value
    target = tmp_path / "settings.yaml"
    target.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return target


def test_active_security_plugin_registers_cleanup(tmp_path: Path) -> None:
    from bootstrap.composition import _build_security_components, create_runtime_cli
    from config.settings import Settings

    settings = Settings.from_yaml(_settings_with(tmp_path))
    registry = FakeCleanupRegistry()

    create_runtime_cli(
        settings,
        domain_components=_build_security_components(settings),
        cleanup_registry=registry,  # type: ignore[arg-type]
    )

    assert "docker-by-pid" in registry.handlers
    assert callable(registry.handlers["docker-by-pid"])


def test_no_domain_plugin_skips_cleanup_registration(tmp_path: Path) -> None:
    from bootstrap.composition import create_runtime_cli
    from config.settings import Settings

    settings = Settings.from_yaml(
        _settings_with(tmp_path, domain_plugins={"security": {"enabled": False}})
    )
    registry = FakeCleanupRegistry()

    create_runtime_cli(settings, cleanup_registry=registry)  # type: ignore[arg-type]

    assert "docker-by-pid" not in registry.handlers


def test_missing_registry_skips_cleanup_registration(tmp_path: Path) -> None:
    from bootstrap.composition import _build_security_components, create_runtime_cli
    from config.settings import Settings

    settings = Settings.from_yaml(_settings_with(tmp_path))

    create_runtime_cli(
        settings,
        domain_components=_build_security_components(settings),
        cleanup_registry=None,
    )
