"""Bootstrap wiring tests for flat-mode composition.

Verifies the ``__post_init__`` guard on ``ApplicationConfig`` and that
``create_runtime_cli`` threads ``WorkerPort`` + ``FlatInvariantBuilder``
into ``AgentExecutionService`` when ``orchestration.mode='flat'``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from config import (
    BossConfig,
    ConcurrencyConfig,
    ManagerConfig,
    ToolCallingConfig,
    TopologyConfig,
)


if TYPE_CHECKING:
    from uuid import UUID

    from core.application.execution_service import FlatModeBundle
    from core.application.run_invariants import (
        TaskPromptSpec,
        TimeoutBudget,
        ToolPolicy,
        WorkerResult,
        WorkspaceSpec,
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = REPO_ROOT / "config" / "config.yaml"


@pytest.fixture(autouse=True)
def _postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")


def _load_base_config() -> dict[str, Any]:
    return yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))


def _add_required_models(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("boss", {})["model"] = "test-boss-model"
    payload.setdefault("manager", {})["model"] = "test-manager-model"
    payload.setdefault("worker", {})["model"] = "test-worker-model"
    return payload


def _settings_with(tmp_path: Path, **overrides: Any) -> Path:
    """Write a settings YAML overlay onto ``config.yaml`` and return the path."""
    payload = _add_required_models(_load_base_config())
    for top_key, sub in overrides.items():
        existing = payload.get(top_key, {})
        if (
            top_key == "worker"
            and isinstance(sub, dict)
            and "tool" in sub
            and isinstance(existing, dict)
        ):
            existing = {k: v for k, v in existing.items() if k != "tool_params"}
        if isinstance(existing, dict) and isinstance(sub, dict):
            existing.update(sub)
            payload[top_key] = existing
        else:
            payload[top_key] = sub
    target = tmp_path / "settings.yaml"
    target.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return target


def _make_noop_invariant_builder():
    """Return a callable matching the ``FlatInvariantBuilder`` protocol."""
    from core.application.execution_service import FlatModeBundle
    from core.application.run_invariants import (
        TaskPromptSpec,
        TimeoutBudget,
        ToolPolicy,
        WorkspaceSpec,
    )

    def _builder(
        *,
        task: str,
        domain_context: object | None,
        run_dir: Path,
    ) -> FlatModeBundle:
        del domain_context
        return FlatModeBundle(
            spec=TaskPromptSpec(rendered_prompt="x", prompt_sha="x", task=task),
            tool_policy=ToolPolicy(allowed=(), disallowed=(), allowed_bash_commands=()),
            timeouts=TimeoutBudget(per_worker_call=10, per_run_total=10),
            workspace=WorkspaceSpec(root=run_dir),
        )

    return _builder


class _FakeWorker:
    """Minimal ``WorkerPort`` for guard tests."""

    async def run_task(
        self,
        *,
        run_id: UUID,
        spec: TaskPromptSpec,
        tool_policy: ToolPolicy,
        timeouts: TimeoutBudget,
        workspace: WorkspaceSpec,
    ) -> WorkerResult:
        del run_id, spec, tool_policy, timeouts, workspace
        raise NotImplementedError


def _base_app_config_kwargs() -> dict[str, Any]:
    return {
        "topology": TopologyConfig(
            max_depth=-1, max_children_per_node=-1, max_total_agents=-1
        ),
        "concurrency": ConcurrencyConfig(max_concurrent_workers=-1),
        "tool_calling": ToolCallingConfig(),
        "max_retries": 3,
        "poll_interval": 0.5,
        "boss_config": BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        "manager_config": ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        "output_directory": "./test_output",
        "default_worker_tool": "claude_code",
    }


def test_application_config_flat_mode_requires_worker() -> None:
    """ApplicationConfig refuses construction when mode='flat' but flat_worker is None."""
    from bootstrap import ApplicationConfig

    # Given: flat mode with a builder but no worker.
    builder = _make_noop_invariant_builder()

    # When + Then: constructing raises ValueError naming both required fields.
    with pytest.raises(ValueError, match="flat_worker"):
        ApplicationConfig(
            **_base_app_config_kwargs(),
            mode="flat",
            flat_worker=None,
            flat_invariant_builder=builder,
        )


def test_application_config_flat_mode_requires_invariant_builder() -> None:
    """ApplicationConfig refuses flat mode without a FlatInvariantBuilder."""
    from bootstrap import ApplicationConfig

    # Given: flat mode with a worker but no builder.
    worker = _FakeWorker()

    # When + Then: constructing raises ValueError naming both required fields.
    with pytest.raises(ValueError, match="flat_invariant_builder"):
        ApplicationConfig(
            **_base_app_config_kwargs(),
            mode="flat",
            flat_worker=worker,
            flat_invariant_builder=None,
        )


def test_application_config_hierarchical_mode_does_not_require_flat_fields() -> None:
    """Hierarchical mode (default) does NOT need flat-only fields."""
    from bootstrap import ApplicationConfig

    # Given + When: a hierarchical-mode ApplicationConfig with defaults.
    config = ApplicationConfig(**_base_app_config_kwargs())

    # Then: defaults pass through unchanged and no guard fires.
    assert config.mode == "hierarchical"
    assert config.flat_worker is None
    assert config.flat_invariant_builder is None


def test_create_runtime_cli_wires_flat_mode_for_claude_code(tmp_path: Path) -> None:
    """``orchestration.mode='flat'`` threads WorkerPort + FlatInvariantBuilder."""
    from bootstrap.composition import create_runtime_cli
    from config.settings import Settings
    from core.ports.worker_port import WorkerPort

    # Given: a settings overlay enabling flat mode with the claude_code worker.
    settings_path = _settings_with(
        tmp_path,
        worker={
            "model": "claude-sonnet-4-20250514",
            "tool": "claude_code",
            "timeout": 300,
            "max_iterations_per_run": 20,
            "tool_params": {
                "claude_code": {
                    "output_format": "stream-json",
                    "include_partial_messages": True,
                    "max_turns": 40,
                }
            },
        },
        orchestration={"mode": "flat"},
    )
    settings = Settings.from_yaml(settings_path)

    # When: building the CLI through the composition root.
    cli = create_runtime_cli(settings)

    # Then: the execution service runs in flat mode with both collaborators wired.
    service = cli.execution_service
    assert service._config.mode == "flat"
    assert service._flat_worker is not None
    assert isinstance(service._flat_worker, WorkerPort)
    assert service._flat_invariant_builder is not None
    assert callable(service._flat_invariant_builder)


def test_create_runtime_cli_leaves_hierarchical_mode_default(tmp_path: Path) -> None:
    """Hierarchical mode (the default) does NOT instantiate flat-only collaborators."""
    from bootstrap.composition import create_runtime_cli
    from config.settings import Settings

    # Given: a settings overlay that keeps orchestration.mode at its default.
    settings_path = _settings_with(tmp_path)
    settings = Settings.from_yaml(settings_path)

    # When: building the CLI.
    cli = create_runtime_cli(settings)

    # Then: the execution service runs in hierarchical mode and flat fields are unset.
    service = cli.execution_service
    assert service._config.mode == "hierarchical"
    assert service._flat_worker is None
    assert service._flat_invariant_builder is None


# BUG-TOOL1 regression: composition.py used to construct InfrastructureConfig
# without ``worker_allowed_tools`` / ``worker_disallowed_tools``, so the
# hierarchical adapters silently fell back to each SDK's hardcoded defaults
# and ignored the YAML. The two tests below pin the wiring so a future
# regression at the composition.py boundary fails loudly here.


def _custom_tool_policy_settings(tmp_path: Path, *, tool: str, model: str) -> Path:
    return _settings_with(
        tmp_path,
        worker={
            "model": model,
            "tool": tool,
            "timeout": 300,
            "max_iterations_per_run": 20,
            # Use a list distinct from every adapter's hardcoded default
            # (SDKAdapterConfig defaults to no MultiEdit; OpenHandsAdapter
            # defaults to ``["*"]``) so the assertion fails if either
            # fallback silently re-enters.
            "allowed_tools": [
                "Read",
                "Write",
                "Edit",
                "MultiEdit",
                "Bash",
                "Glob",
                "Grep",
            ],
            "disallowed_tools": ["WebSearch", "WebFetch"],
        },
    )


def test_create_runtime_cli_threads_allowed_tools_to_claude_sdk(tmp_path: Path) -> None:
    """B-cell wiring: YAML ``worker.allowed_tools`` reaches the Claude SDK adapter."""
    from bootstrap.composition import create_runtime_cli
    from config.settings import Settings
    from infrastructure.adapters.worker import ClaudeAgentSDKAdapter

    # Given: a hierarchical claude_code settings overlay with an explicit allowlist.
    settings_path = _custom_tool_policy_settings(
        tmp_path, tool="claude_code", model="claude-sonnet-4-20250514"
    )
    settings = Settings.from_yaml(settings_path)

    # When: building the CLI.
    cli = create_runtime_cli(settings)

    # Then: the SDK adapter received the YAML policy verbatim, not its hardcoded default.
    worker_port = cli.execution_service._orchestrator._worker_port
    assert isinstance(worker_port, ClaudeAgentSDKAdapter)
    assert worker_port.config.allowed_tools == [
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "Bash",
        "Glob",
        "Grep",
    ]
    assert worker_port.config.disallowed_tools == ["WebSearch", "WebFetch"]


def test_create_runtime_cli_threads_allowed_tools_to_openhands(tmp_path: Path) -> None:
    """C-cell wiring: YAML ``worker.allowed_tools`` reaches the OpenHands adapter."""
    from bootstrap.composition import create_runtime_cli
    from config.settings import Settings
    from infrastructure.adapters.worker import OpenHandsAdapter

    # Given: a hierarchical openhands settings overlay with an explicit allowlist.
    settings_path = _custom_tool_policy_settings(
        tmp_path, tool="openhands", model="openai/gpt-4o-mini"
    )
    overlay = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    overlay["worker"]["allowed_tools"] = [
        "file_editor",
        "glob",
        "grep",
        "valgrind_run",
        "klee_run",
    ]
    overlay["worker"]["disallowed_tools"] = ["terminal", "browser_tool_set"]
    overlay["worker"]["tool_params"] = {"openhands": {}}
    settings_path.write_text(yaml.safe_dump(overlay), encoding="utf-8")
    settings = Settings.from_yaml(settings_path)

    # When: building the CLI.
    cli = create_runtime_cli(settings)

    # Then: the OpenHands adapter received the YAML policy verbatim, not ``["*"]``.
    worker_port = cli.execution_service._orchestrator._worker_port
    assert isinstance(worker_port, OpenHandsAdapter)
    assert worker_port.allowed_tools == [
        "file_editor",
        "glob",
        "grep",
        "valgrind_run",
        "klee_run",
    ]
    assert worker_port.disallowed_tools == ["terminal", "browser_tool_set"]
