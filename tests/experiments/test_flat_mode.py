"""Unit tests for flat-mode dispatch through ``aris`` and the bootstrap composition.

These tests stub out subprocess calls and the database; they exercise the
runner-to-harness path and the bootstrap-side wiring of WorkerPort + invariant
builder. The heavier end-to-end test (a full ExecutionService run with a
fake worker) lives in ``tests/integration/test_flat_mode_baseline.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import yaml

from experiments.shared import harness
from experiments.shared.runners import aris as aris_module, get as get_runner


if TYPE_CHECKING:
    from plugins.security import CVEInstance


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = REPO_ROOT / "config" / "config.yaml"


def _prompt_builder():
    """Construct a PromptBuilder for flat-mode tests.

    Mirrors the construction in ``bootstrap/composition.py::create_runtime_cli``:
    template_dir=``prompts``, default_tool=``claude_code``, strategy=SecBench.
    """
    from core.application.services import PromptBuilder
    from plugins.security import SecBenchPromptStrategy

    return PromptBuilder(
        template_dir=str(REPO_ROOT / "prompts"),
        default_tool="claude_code",
        strategy=SecBenchPromptStrategy(),
    )


@pytest.fixture(autouse=True)
def _postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")


def _load_base_config() -> dict:
    return yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))


def _settings_with(tmp_path: Path, **overrides: dict) -> Path:
    """Write a settings YAML and return its path."""
    payload = _load_base_config()
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


def test_aris_runner_delegates_to_harness_run_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    """aris.run forwards every dispatch through ``harness.run_ours`` regardless of cell."""
    # Given: a stubbed run_ours that captures its arguments and returns a sentinel.
    sentinel = uuid4()
    captured: dict[str, object] = {}

    def _fake_run_ours(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(harness, "run_ours", _fake_run_ours)

    # When: dispatching an A-cell (flat-mode) and a B-cell (hierarchical-mode)
    # through the same runner.
    aris = get_runner("aris")
    config_path = Path("/tmp/cell.yaml")  # noqa: S108 — test sentinel only.
    flat_result = aris.run(
        study_id="study-x",
        cell="A1",
        task="cve-flat",
        replicate=0,
        config=config_path,
        context_file=Path("ignored.json"),
    )
    hier_result = aris.run(
        study_id="study-x",
        cell="B2",
        task="cve-hier",
        replicate=2,
        config=config_path,
        context_file=Path("ignored.json"),
    )

    # Then: both cells came back with the run_ours sentinel and forwarded the
    # config + replicate-as-attempt mapping unchanged.
    assert flat_result == sentinel
    assert hier_result == sentinel
    # The capture only retains the LAST call; assert it carries the B-cell's
    # arguments (a sanity check that we did make two distinct calls).
    assert captured["cell"] == "B2"
    assert captured["task"] == "cve-hier"
    assert captured["attempt"] == 2
    assert captured["config"] == config_path


def test_aris_module_no_longer_carries_legacy_variant_table() -> None:
    """The legacy ``_LEGACY_VARIANTS`` mapping is removed in PR 4b."""
    assert not hasattr(aris_module, "_LEGACY_VARIANTS")


def _demo_cve_instance() -> CVEInstance:
    from plugins.security import CVEInstance

    return CVEInstance(
        instance_id="demo.cve-9999-0001",
        repo="demo/p",
        project_name="demo",
        lang="c",
        work_dir="/src/demo",
        sanitizer="address",
        bug_description="Heap overflow in parse() at src/demo.c:42.",
        base_commit="a" * 40,
    )


def test_flat_invariant_builder_consumes_settings_overlay(tmp_path: Path) -> None:
    """The bootstrap-side invariant builder picks up overlaid settings.

    A-cell tool policy is expressed at the top level in YAML — A2 sets
    ``worker.disallowed_tools: ["Task"]`` to suppress Claude's Task tool
    (that's the entire A1-vs-A2 contrast). The closure reads from there
    directly so the policy flows through to the ``ClaudeCodeWorker`` CLI flags.
    """
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings

    # Given: a settings overlay that pins per-worker-call to 450s and the
    # top-level tool allow/deny lists used by A1/A2.
    settings_path = _settings_with(
        tmp_path,
        worker={
            "model": "gpt-4o",
            "tool": "claude_code",
            "allowed_tools": ["Bash"],
            "disallowed_tools": ["WebFetch"],
            "timeout": 450,
            "max_iterations_per_run": 20,
            "tool_params": {
                "claude_code": {
                    "output_format": "stream-json",
                    "include_partial_messages": True,
                    "max_turns": 40,
                }
            },
        },
        orchestration={"mode": "flat", "max_run_duration_seconds": 7200},
    )
    settings = Settings.from_yaml(settings_path)
    builder = _make_flat_invariant_builder(
        settings=settings, prompt_builder=_prompt_builder(), context_file=None
    )

    # When: invoking the closure for a synthetic run.
    run_dir = tmp_path / "runs" / "abc"
    run_dir.mkdir(parents=True)
    bundle = builder(task="t1", domain_context=_demo_cve_instance(), run_dir=run_dir)

    # Then: the bundle reflects the overridden settings.
    assert bundle.timeouts.per_worker_call == 450
    assert bundle.timeouts.per_run_total == 7200
    assert bundle.tool_policy.allowed == ("Bash",)
    assert bundle.tool_policy.disallowed == ("WebFetch",)
    assert bundle.workspace.root == run_dir
    assert "demo.cve-9999-0001" in bundle.spec.rendered_prompt


def test_flat_invariant_builder_rejects_missing_cve_instance(tmp_path: Path) -> None:
    """Cell A's flat dispatch demands a CVEInstance domain_context.

    Without it, the unified prompt renderer cannot produce the input
    block. Fail fast at builder time rather than ship a context-free
    prompt downstream.
    """
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings

    settings_path = _settings_with(tmp_path, orchestration={"mode": "flat"})
    settings = Settings.from_yaml(settings_path)
    builder = _make_flat_invariant_builder(
        settings=settings, prompt_builder=_prompt_builder(), context_file=None
    )

    run_dir = tmp_path / "runs" / "x"
    run_dir.mkdir(parents=True)
    with pytest.raises(ValueError, match="CVEInstance"):
        builder(task="task-1", domain_context=None, run_dir=run_dir)


def test_build_flat_worker_picks_claude_code(tmp_path: Path) -> None:
    """``_build_flat_worker`` returns a ClaudeCodeWorker when worker.tool=claude_code."""
    from bootstrap.composition import _build_flat_worker
    from config.settings import Settings
    from infrastructure.workers import ClaudeCodeWorker

    settings_path = _settings_with(
        tmp_path,
        worker={
            "model": "gpt-4o",
            "tool": "claude_code",
            "timeout": 300,
            "max_iterations_per_run": 20,
            "tool_params": {
                "claude_code": {
                    "allowed_tools": ["Bash"],
                    "disallowed_tools": [],
                    "output_format": "stream-json",
                    "include_partial_messages": True,
                    "max_turns": 40,
                }
            },
        },
        orchestration={"mode": "flat"},
    )
    settings = Settings.from_yaml(settings_path)

    worker = _build_flat_worker(settings)

    assert isinstance(worker, ClaudeCodeWorker)


def test_build_flat_worker_rejects_google_adk(tmp_path: Path) -> None:
    """google_adk has no flat-mode adapter today; bootstrap fails fast."""
    from bootstrap.composition import _build_flat_worker
    from config.settings import Settings

    settings_path = _settings_with(
        tmp_path,
        worker={
            "model": "gemini-1.5-pro",
            "tool": "google_adk",
            "timeout": 300,
            "max_iterations_per_run": 20,
            "tool_params": {"google_adk": {}},
        },
        orchestration={"mode": "flat"},
    )
    settings = Settings.from_yaml(settings_path)

    with pytest.raises(NotImplementedError, match="google_adk"):
        _build_flat_worker(settings)


def test_flat_invariant_builder_pins_a2_task_denial_against_default_tool_params(
    tmp_path: Path,
) -> None:
    """A2's ``worker.disallowed_tools: ["Task"]`` must reach the worker.

    A2's `tool_params.claude_code` slot omits `disallowed_tools` (defaults to
    `[]`), and the entire A1-vs-A2 contrast hinges on the top-level denial
    reaching the Claude CLI. A regression that re-prefers the nested slot
    would silently make A1 ≡ A2.
    """
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings

    # Given: an A2-shaped settings overlay — Task denied at the top level,
    # the nested claude_code slot deliberately omits ``disallowed_tools``.
    settings_path = _settings_with(
        tmp_path,
        worker={
            "model": "claude-sonnet-4-6",
            "tool": "claude_code",
            "allowed_tools": ["*"],
            "disallowed_tools": ["Task"],
            "timeout": 300,
            "max_iterations_per_run": 20,
            "tool_params": {
                "claude_code": {
                    "output_format": "stream-json",
                    "include_partial_messages": True,
                    "max_turns": 20,
                    "use_global_config": False,
                }
            },
        },
        orchestration={"mode": "flat"},
    )
    settings = Settings.from_yaml(settings_path)
    builder = _make_flat_invariant_builder(
        settings=settings, prompt_builder=_prompt_builder(), context_file=None
    )

    # When: invoking the closure on a valid CVE context.
    run_dir = tmp_path / "runs" / "a2"
    run_dir.mkdir(parents=True)
    bundle = builder(task="t1", domain_context=_demo_cve_instance(), run_dir=run_dir)

    # Then: Task is denied; allow-everything is preserved.
    assert bundle.tool_policy.allowed == ("*",)
    assert bundle.tool_policy.disallowed == ("Task",)


def test_flat_invariant_builder_renders_a1_prompt_with_subagent_note(tmp_path: Path) -> None:
    """A1's flat prompt carries the ``FLAT_SUBAGENT_NOTE`` capability hint."""
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings
    from core.application.services.prompt.prompt_builder import FLAT_SUBAGENT_NOTE

    # Given: an A1-shaped settings overlay — Task tool allowed.
    settings_path = _settings_with(
        tmp_path,
        worker={
            "model": "claude-sonnet-4-6",
            "tool": "claude_code",
            "allowed_tools": ["*"],
            "disallowed_tools": [],
            "timeout": 300,
            "max_iterations_per_run": 20,
            "tool_params": {
                "claude_code": {
                    "output_format": "stream-json",
                    "include_partial_messages": True,
                    "max_turns": 20,
                    "use_global_config": False,
                }
            },
        },
        orchestration={"mode": "flat"},
    )
    settings = Settings.from_yaml(settings_path)
    builder = _make_flat_invariant_builder(
        settings=settings, prompt_builder=_prompt_builder(), context_file=None
    )

    # When: the closure renders for a CVE.
    run_dir = tmp_path / "runs" / "a1"
    run_dir.mkdir(parents=True)
    bundle = builder(task="t1", domain_context=_demo_cve_instance(), run_dir=run_dir)

    # Then: the 4-phase pipeline AND the subagent note are present.
    assert "## Vulnerability Reproduction — 4-Phase Process" in bundle.spec.rendered_prompt
    assert FLAT_SUBAGENT_NOTE in bundle.spec.rendered_prompt


def test_flat_invariant_builder_renders_a2_prompt_without_subagent_note(tmp_path: Path) -> None:
    """A2's flat prompt omits the ``FLAT_SUBAGENT_NOTE`` because Task is denied."""
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings
    from core.application.services.prompt.prompt_builder import FLAT_SUBAGENT_NOTE

    # Given: an A2-shaped settings overlay — Task tool denied.
    settings_path = _settings_with(
        tmp_path,
        worker={
            "model": "claude-sonnet-4-6",
            "tool": "claude_code",
            "allowed_tools": ["*"],
            "disallowed_tools": ["Task"],
            "timeout": 300,
            "max_iterations_per_run": 20,
            "tool_params": {
                "claude_code": {
                    "output_format": "stream-json",
                    "include_partial_messages": True,
                    "max_turns": 20,
                    "use_global_config": False,
                }
            },
        },
        orchestration={"mode": "flat"},
    )
    settings = Settings.from_yaml(settings_path)
    builder = _make_flat_invariant_builder(
        settings=settings, prompt_builder=_prompt_builder(), context_file=None
    )

    # When: the closure renders for a CVE.
    run_dir = tmp_path / "runs" / "a2"
    run_dir.mkdir(parents=True)
    bundle = builder(task="t1", domain_context=_demo_cve_instance(), run_dir=run_dir)

    # Then: the pipeline is present but the subagent note is absent.
    assert "## Vulnerability Reproduction — 4-Phase Process" in bundle.spec.rendered_prompt
    assert FLAT_SUBAGENT_NOTE not in bundle.spec.rendered_prompt
