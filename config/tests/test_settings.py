"""Tests for ``Settings`` schema, strict mode, and tagged-union validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from config.settings import Settings


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = REPO_ROOT / "config" / "config.yaml"


@pytest.fixture(autouse=True)
def _postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")


def _load_base_config() -> dict:
    return yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))


def _write_yaml(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def test_extra_forbid_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    """An unknown top-level YAML key raises a ValidationError."""

    # Given: a copy of the base config with one stray top-level field added.
    payload = _load_base_config()
    payload["garbage_field"] = 1
    target = tmp_path / "extra.yaml"
    _write_yaml(target, payload)

    # When/Then: from_yaml raises a ValidationError mentioning the stray field.
    with pytest.raises(ValidationError) as exc_info:
        Settings.from_yaml(target)
    msg = str(exc_info.value)
    assert "garbage_field" in msg
    assert "extra_forbidden" in msg


def test_extra_forbid_rejects_unknown_nested_field(tmp_path: Path) -> None:
    """An unknown field inside a nested config (worker.tool_params.claude_code) raises."""

    # Given: a config that selects claude_code with a stray subfield.
    payload = _load_base_config()
    payload["worker"]["tool"] = "claude_code"
    payload["worker"]["tool_params"] = {"claude_code": {"foo_bar_baz": 1}}
    target = tmp_path / "nested.yaml"
    _write_yaml(target, payload)

    # When/Then: validation rejects the stray nested field.
    with pytest.raises(ValidationError) as exc_info:
        Settings.from_yaml(target)
    msg = str(exc_info.value)
    assert "foo_bar_baz" in msg
    assert "extra_forbidden" in msg


def test_tool_params_validation_claude_code_null_raises(tmp_path: Path) -> None:
    """Selecting claude_code with tool_params.claude_code=None raises."""

    # Given: tool=claude_code but matching tool_params slot explicitly null.
    payload = _load_base_config()
    payload["worker"]["tool"] = "claude_code"
    payload["worker"]["tool_params"] = {"claude_code": None}
    target = tmp_path / "null_cc.yaml"
    _write_yaml(target, payload)

    # When/Then: the after-validator surfaces the missing tool params.
    with pytest.raises(ValidationError) as exc_info:
        Settings.from_yaml(target)
    assert "tool_params.claude_code" in str(exc_info.value)


def test_tool_params_validation_openhands_null_raises(tmp_path: Path) -> None:
    """Selecting openhands with tool_params.openhands=None raises."""

    # Given: tool=openhands but matching slot explicitly null.
    payload = _load_base_config()
    payload["worker"]["tool"] = "openhands"
    payload["worker"]["tool_params"] = {"openhands": None}
    target = tmp_path / "null_oh.yaml"
    _write_yaml(target, payload)

    # When/Then: the after-validator surfaces the missing tool params.
    with pytest.raises(ValidationError) as exc_info:
        Settings.from_yaml(target)
    assert "tool_params.openhands" in str(exc_info.value)


def test_default_orchestration_mode_is_hierarchical() -> None:
    """The base config defaults to mode=hierarchical."""

    # When: loading the repo's base config.
    settings = Settings.from_yaml(BASE_CONFIG)

    # Then: mode is hierarchical (preserves today's behavior).
    assert settings.orchestration.mode == "hierarchical"


def test_default_domain_plugin_is_security_for_base_config() -> None:
    """The base config explicitly declares domain.plugin=security."""

    # When: loading the repo's base config.
    settings = Settings.from_yaml(BASE_CONFIG)

    # Then: the active domain plugin is the security one.
    assert settings.domain.plugin == "security"
    assert settings.domain.params == {}




def test_explicit_tool_params_override_defaults(tmp_path: Path) -> None:
    """Explicit tool_params values are preserved (no auto-fill clobber)."""

    # Given: a config where openhands.timeout_seconds is explicitly set.
    payload = _load_base_config()
    payload["worker"]["tool_params"] = {
        "openhands": {"image": "custom:latest", "timeout_seconds": 42, "max_iterations_per_run": 3}
    }
    target = tmp_path / "explicit.yaml"
    _write_yaml(target, payload)

    # When: loaded.
    settings = Settings.from_yaml(target)

    # Then: explicit values win.
    assert settings.worker.tool_params.openhands is not None
    assert settings.worker.tool_params.openhands.image == "custom:latest"
    assert settings.worker.tool_params.openhands.timeout_seconds == 42
    assert settings.worker.tool_params.openhands.max_iterations_per_run == 3


def test_tool_params_rejects_inactive_slot_payload(tmp_path: Path) -> None:
    """A populated slot for a non-active tool surfaces as a validation error."""

    # Given: an overlay selecting openhands but also carrying a claude_code payload.
    overlay = tmp_path / "cross_tool.yaml"
    _write_yaml(
        overlay,
        {
            "extends": str(BASE_CONFIG.relative_to(REPO_ROOT)),
            "overrides": {
                "worker.tool": "openhands",
                "worker.tool_params": {
                    "openhands": {
                        "image": "x:latest",
                        "timeout_seconds": 10,
                        "max_iterations_per_run": 5,
                    },
                    "claude_code": {"disallowed_tools": ["Task"]},
                },
            },
        },
    )

    # When/Then: validation rejects the stray claude_code slot.
    with pytest.raises(ValidationError) as exc_info:
        Settings.from_yaml(overlay)
    msg = str(exc_info.value)
    assert "populated slots for inactive tools" in msg
    assert "claude_code" in msg


def test_overlay_yaml_is_resolved_by_from_yaml(tmp_path: Path) -> None:
    """from_yaml transparently resolves extends:/overrides: overlays."""

    # Given: an overlay that extends the base config and flips orchestration.mode.
    overlay = tmp_path / "overlay.yaml"
    _write_yaml(
        overlay,
        {
            "extends": str(BASE_CONFIG.relative_to(REPO_ROOT)),
            "overrides": {"orchestration.mode": "flat"},
        },
    )

    # When: from_yaml loads the overlay.
    settings = Settings.from_yaml(overlay)

    # Then: mode reflects the override; everything else is inherited.
    assert settings.orchestration.mode == "flat"
    assert settings.worker.tool == "openhands"
