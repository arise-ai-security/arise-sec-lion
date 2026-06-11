"""Tests for ``Settings`` schema, strict mode, and tagged-union validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from config.settings import ApiSettings, Settings


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = REPO_ROOT / "config" / "config.yaml"
STUDY_CONFIG_DIR = REPO_ROOT / "experiments" / "shared" / "templates" / "study" / "configs"


@pytest.fixture(autouse=True)
def _postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")


def _load_base_config() -> dict:
    return yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))


def _add_required_models(payload: dict) -> dict:
    payload.setdefault("boss", {})["model"] = "test-boss-model"
    payload.setdefault("manager", {})["model"] = "test-manager-model"
    payload.setdefault("worker", {})["model"] = "test-worker-model"
    return payload


def _write_yaml(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def test_extra_forbid_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    """An unknown top-level YAML key raises a ValidationError."""

    # Given: a copy of the base config with one stray top-level field added.
    payload = _add_required_models(_load_base_config())
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
    payload = _add_required_models(_load_base_config())
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
    payload = _add_required_models(_load_base_config())
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
    payload = _add_required_models(_load_base_config())
    payload["worker"]["tool"] = "openhands"
    payload["worker"]["tool_params"] = {"openhands": None}
    target = tmp_path / "null_oh.yaml"
    _write_yaml(target, payload)

    # When/Then: the after-validator surfaces the missing tool params.
    with pytest.raises(ValidationError) as exc_info:
        Settings.from_yaml(target)
    assert "tool_params.openhands" in str(exc_info.value)


def test_shared_worker_session_defaults_false_and_accepts_true(tmp_path: Path) -> None:
    """``orchestration.shared_worker_session`` defaults False; YAML can enable it."""

    # Given: a base config without mentioning the flag.
    payload = _add_required_models(_load_base_config())
    target = tmp_path / "swss_default.yaml"
    _write_yaml(target, payload)

    # When/Then: it defaults False.
    assert Settings.from_yaml(target).orchestration.shared_worker_session is False

    # Given: the same config with the flag enabled.
    payload["orchestration"]["shared_worker_session"] = True
    target_on = tmp_path / "swss_on.yaml"
    _write_yaml(target_on, payload)

    # When/Then: it round-trips as True.
    assert Settings.from_yaml(target_on).orchestration.shared_worker_session is True


def test_openhands_enable_subagents_defaults_false_and_accepts_true(tmp_path: Path) -> None:
    """``worker.tool_params.openhands.enable_subagents`` defaults False; YAML can enable it."""

    # Given: an openhands worker with an empty tool_params slot (N1-shaped).
    payload = _add_required_models(_load_base_config())
    payload["worker"]["tool"] = "openhands"
    payload["worker"]["tool_params"] = {"openhands": {}}
    target = tmp_path / "oh_default.yaml"
    _write_yaml(target, payload)

    # When: loading settings without mentioning enable_subagents.
    settings = Settings.from_yaml(target)

    # Then: the flag defaults to False.
    assert settings.worker.tool_params.openhands is not None
    assert settings.worker.tool_params.openhands.enable_subagents is False

    # Given: the same config with the flag enabled (N2-shaped).
    payload["worker"]["tool_params"] = {"openhands": {"enable_subagents": True}}
    target_enabled = tmp_path / "oh_enabled.yaml"
    _write_yaml(target_enabled, payload)

    # When: loading the N2-shaped settings.
    enabled = Settings.from_yaml(target_enabled)

    # Then: the flag round-trips as True.
    assert enabled.worker.tool_params.openhands is not None
    assert enabled.worker.tool_params.openhands.enable_subagents is True


def test_base_config_has_no_default_agent_model_names() -> None:
    """Agent model names must be supplied by experiment overlays."""

    payload = _load_base_config()

    assert "model" not in payload["boss"]
    assert "model" not in payload["manager"]
    assert "model" not in payload["worker"]
    assert payload["format_repairer"]["model"] == "gpt-5.4-mini"


def test_base_config_without_models_fails() -> None:
    """Loading config/config.yaml directly fails until models are supplied."""

    with pytest.raises(ValidationError) as exc_info:
        Settings.from_yaml(BASE_CONFIG)

    msg = str(exc_info.value)
    assert "boss.model" in msg
    assert "manager.model" in msg
    assert "worker.model" in msg


def test_api_settings_loads_base_config_without_agent_models() -> None:
    """The query API can boot from base config without experiment model names."""

    settings = ApiSettings.from_yaml(BASE_CONFIG)

    assert settings.database.password == os.environ["POSTGRES_PASSWORD"]
    assert settings.worker.tool == "openhands"
    assert settings.worker.timeout == 1000
    assert settings.orchestration.max_retries == 3


def test_format_repairer_model_is_required_from_config(tmp_path: Path) -> None:
    """The repairer has a YAML default, not a source-code fallback."""

    payload = _add_required_models(_load_base_config())
    del payload["format_repairer"]["model"]
    target = tmp_path / "repairer.yaml"
    _write_yaml(target, payload)

    with pytest.raises(ValidationError) as exc_info:
        Settings.from_yaml(target)

    assert "format_repairer.model" in str(exc_info.value)


def test_default_orchestration_mode_is_hierarchical(tmp_path: Path) -> None:
    """The base config defaults to mode=hierarchical."""

    # Given: the repo's base config plus required experiment-owned models.
    target = tmp_path / "config.yaml"
    _write_yaml(target, _add_required_models(_load_base_config()))

    # When: loading the config.
    settings = Settings.from_yaml(target)

    # Then: mode is hierarchical (preserves today's behavior).
    assert settings.orchestration.mode == "hierarchical"


def test_openhands_tool_params_accepts_empty_backend_slot(tmp_path: Path) -> None:
    """OpenHands backend params may omit MCP tools; top-level worker fields drive limits."""

    # Given: an explicit empty openhands slot.
    payload = _add_required_models(_load_base_config())
    payload["worker"]["tool_params"] = {"openhands": {}}
    target = tmp_path / "explicit.yaml"
    _write_yaml(target, payload)

    # When: loaded.
    settings = Settings.from_yaml(target)

    # Then: the slot is populated, but no duplicate backend knobs exist.
    assert settings.worker.tool_params.openhands is not None
    assert settings.worker.tool_params.openhands.mcp_tools == []
    assert settings.worker.timeout == payload["worker"]["timeout"]
    assert settings.worker.max_iterations_per_run == payload["worker"]["max_iterations_per_run"]


def test_openhands_tool_params_rejects_removed_duplicate_fields(tmp_path: Path) -> None:
    """OpenHands timeout/iteration config must stay at top-level worker fields."""

    # Given: the retired OpenHands-specific timeout/image fields.
    payload = _add_required_models(_load_base_config())
    payload["worker"]["tool_params"] = {
        "openhands": {"image": "custom:latest", "timeout_seconds": 42, "max_iterations_per_run": 3}
    }
    target = tmp_path / "duplicate_openhands.yaml"
    _write_yaml(target, payload)

    # When/Then: strict validation rejects the stale fields.
    with pytest.raises(ValidationError) as exc_info:
        Settings.from_yaml(target)

    msg = str(exc_info.value)
    assert "image" in msg
    assert "timeout_seconds" in msg
    assert "max_iterations_per_run" in msg


def test_tool_params_rejects_inactive_slot_payload(tmp_path: Path) -> None:
    """A populated slot for a non-active tool surfaces as a validation error."""

    # Given: an overlay selecting openhands but also carrying a claude_code payload.
    overlay = tmp_path / "cross_tool.yaml"
    _write_yaml(
        overlay,
        {
            "extends": str(BASE_CONFIG.relative_to(REPO_ROOT)),
            "overrides": {
                "boss.model": "test-boss-model",
                "manager.model": "test-manager-model",
                "worker.model": "test-worker-model",
                "worker.tool": "openhands",
                "worker.tool_params": {
                    "openhands": {},
                    "claude_code": {"max_turns": 5},
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
            "overrides": {
                "boss.model": "test-boss-model",
                "manager.model": "test-manager-model",
                "worker.model": "test-worker-model",
                "orchestration.mode": "flat",
            },
        },
    )

    # When: from_yaml loads the overlay.
    settings = Settings.from_yaml(overlay)

    # Then: mode reflects the override; everything else is inherited.
    assert settings.orchestration.mode == "flat"
    assert settings.worker.tool == "openhands"


@pytest.mark.parametrize(
    "config_name",
    ["C1-qwen-noverifier.yaml", "C2-qwen-verifier.yaml"],
)
def test_study_openhands_tool_policy_overrides_base_defaults(config_name: str) -> None:
    """Study C-cells must replace base worker tool defaults."""

    # Given: C-cell overlays pin the SEC-bench MCP tool surface explicitly.
    path = STUDY_CONFIG_DIR / config_name

    # When: Settings loads the real study overlay.
    settings = Settings.from_yaml(path)

    # Then: the study policy takes precedence over the base/default policy.
    assert settings.worker.tool == "openhands"
    assert settings.worker.allowed_tools == [
        "file_editor",
        "glob",
        "grep",
    ]
    assert settings.worker.disallowed_tools == []
    assert settings.worker.tool_params.openhands is not None
    assert settings.worker.tool_params.openhands.mcp_tools == [
        "shell_in_container",
        "valgrind_run",
        "klee_run",
    ]


@pytest.mark.parametrize("config_name", sorted(p.name for p in STUDY_CONFIG_DIR.glob("*.yaml")))
def test_all_study_template_configs_load(config_name: str) -> None:
    """Every committed study template must be a valid Settings overlay."""

    settings = Settings.from_yaml(STUDY_CONFIG_DIR / config_name)

    assert settings.boss.model
    assert settings.manager.model
    assert settings.worker.model


def test_secbench_recon_tool_allowlist_matches_registered_tools() -> None:
    """Configured secbench recon tools must exist and resolve for each role."""

    from core.application.services.toolset.toolset_context import LoopPolicy
    from core.application.services.toolset.toolset_policy_resolver import (
        ToolsetPolicyResolver,
    )
    from core.domain.values.enums import AgentRole
    from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter

    settings = Settings._build_from_config(_add_required_models(_load_base_config()))
    recon = ReconToolAdapter()
    registered = {
        tool["function"]["name"]
        for tool in recon.get_tool_definitions()
    }
    expected = [
        "read_file",
        "search_codebase",
        "get_file_structure",
        "get_symbols_overview",
        "read_symbol",
    ]

    assert set(expected) <= registered

    resolver = ToolsetPolicyResolver(
        toolsets=(recon,),
        config=settings.orchestration.tool_calling.policies.to_raw_dict(),
        default_loop_policy=LoopPolicy(
            max_iterations=settings.orchestration.tool_calling.max_iterations,
            result_char_limit=settings.orchestration.tool_calling.result_char_limit,
        ),
        domain_key="secbench",
    )

    for role in (AgentRole.BOSS, AgentRole.PENDING, AgentRole.MANAGER):
        resolved = resolver.resolve(role)
        names = [tool["function"]["name"] for tool in resolved.tool_definitions]
        assert sorted(names) == sorted(expected)
        assert set(resolved.executors) == set(expected)
        assert resolved.loop_policy.max_iterations == 3


def test_worker_cost_knobs_default_to_legacy() -> None:
    """Every cost knob defaults to legacy behavior — flags must be opted into."""

    # Given: the base config with only the required models added.
    settings = Settings._build_from_config(_add_required_models(_load_base_config()))

    # Then: worker LLM knobs keep the SDK defaults.
    assert settings.worker.reasoning_effort is None
    assert settings.worker.reasoning_effort_overrides == {}

    # Then: orchestration knobs keep byte-identical prompts and today's retry budget.
    orch = settings.orchestration
    assert orch.shared_code_prefix_first is False
    assert orch.shared_code_index is False
    assert orch.shared_code_skip_dir_listings is False
    assert orch.shared_code_render_mode == "append_only"
    assert orch.workspace_listing_dirs is None
    assert orch.workspace_listing_max_entries is None
    assert orch.verification_max_retries == 2
    assert orch.capture_recon_reads is False


def test_worker_cost_knobs_round_trip_from_yaml(tmp_path: Path) -> None:
    """The opted-in shapes load: per-branch efforts, cache key, listing whitelist."""

    # Given: a config enabling every knob the way a new study overlay would.
    payload = _add_required_models(_load_base_config())
    payload["worker"]["tool"] = "openhands"
    payload["worker"]["reasoning_effort"] = "low"
    payload["worker"]["reasoning_effort_overrides"] = {
        "[Exploiter]": "medium",
        "[Fixer]": "medium",
    }
    payload["worker"]["tool_params"] = {"openhands": {"run_scoped_prompt_cache_key": True}}
    payload["orchestration"]["shared_worker_session"] = True
    payload["orchestration"]["shared_code_prefix_first"] = True
    payload["orchestration"]["shared_code_index"] = True
    payload["orchestration"]["shared_code_skip_dir_listings"] = True
    payload["orchestration"]["shared_code_render_mode"] = "latest_only"
    payload["orchestration"]["workspace_listing_dirs"] = ["testcase"]
    payload["orchestration"]["workspace_listing_max_entries"] = 200
    payload["orchestration"]["verification_max_retries"] = 1
    payload["orchestration"]["capture_recon_reads"] = True
    target = tmp_path / "cost_knobs_on.yaml"
    _write_yaml(target, payload)

    # When
    settings = Settings.from_yaml(target)

    # Then
    assert settings.worker.reasoning_effort == "low"
    assert settings.worker.reasoning_effort_overrides["[Exploiter]"] == "medium"
    assert settings.worker.tool_params.openhands is not None
    assert settings.worker.tool_params.openhands.run_scoped_prompt_cache_key is True
    orch = settings.orchestration
    assert orch.shared_code_prefix_first is True
    assert orch.shared_code_index is True
    assert orch.shared_code_skip_dir_listings is True
    assert orch.shared_code_render_mode == "latest_only"
    assert orch.workspace_listing_dirs == ["testcase"]
    assert orch.workspace_listing_max_entries == 200
    assert orch.verification_max_retries == 1
    assert orch.capture_recon_reads is True
