"""SEC-bench flat-mode invariant tests for bootstrap composition.

These tests exercise the security prompt and invariant bundle assembled by the
composition root. Generic runner and worker-adapter wiring tests live with their
owning experiment and bootstrap layers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.application.services import PromptBuilder
from plugins.security import CVEInstance, SecBenchPromptStrategy


REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_CONFIG = REPO_ROOT / "config" / "config.yaml"


def _prompt_builder() -> PromptBuilder:
    """Construct a PromptBuilder for flat-mode tests.

    Mirrors the construction in ``bootstrap/composition.py::create_runtime_cli``:
    template_dir=``prompts``, default_tool=``claude_code``, strategy=SecBench.
    """
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
    payload.setdefault("boss", {})["model"] = "test-boss-model"
    payload.setdefault("manager", {})["model"] = "test-manager-model"
    payload.setdefault("worker", {})["model"] = "test-worker-model"
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


def _demo_cve_instance() -> CVEInstance:
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

    Flat tool policy is expressed at the top level in YAML. The closure reads
    it directly so the policy flows through to the ``ClaudeCodeWorker`` CLI flags.
    """
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings

    # Given: a settings overlay that pins per-worker-call to 450s and the
    # top-level tool allow/deny lists.
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
    # And: the bash-command allowlist derives from security.tools (enabled in base config).
    assert bundle.tool_policy.allowed_bash_commands == ("valgrind", "klee")
    assert bundle.workspace.root == run_dir
    assert "demo.cve-9999-0001" in bundle.spec.rendered_prompt


def test_flat_invariant_builder_rejects_missing_cve_instance(tmp_path: Path) -> None:
    """Flat security dispatch demands a CVEInstance domain_context.

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


def test_flat_invariant_builder_pins_task_denial_against_default_tool_params(
    tmp_path: Path,
) -> None:
    """Top-level ``worker.disallowed_tools: ["Task"]`` must reach the worker.

    The nested `tool_params.claude_code` slot omits `disallowed_tools` (defaults
    to `[]`), so a regression that re-prefers it would lose the top-level denial.
    """
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings

    # Given: Task denied at the top level,
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
    run_dir = tmp_path / "runs" / "task-denied"
    run_dir.mkdir(parents=True)
    bundle = builder(task="t1", domain_context=_demo_cve_instance(), run_dir=run_dir)

    # Then: Task is denied; allow-everything is preserved.
    assert bundle.tool_policy.allowed == ("*",)
    assert bundle.tool_policy.disallowed == ("Task",)


def test_flat_invariant_builder_renders_subagent_note_when_task_allowed(
    tmp_path: Path,
) -> None:
    """A flat prompt carries the subagent note when delegation is allowed."""
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings
    from core.application.services.prompt.prompt_builder import FLAT_SUBAGENT_NOTE

    # Given: the Task tool is allowed.
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
    run_dir = tmp_path / "runs" / "task-allowed"
    run_dir.mkdir(parents=True)
    bundle = builder(task="t1", domain_context=_demo_cve_instance(), run_dir=run_dir)

    # Then: the 4-phase pipeline AND the subagent note are present.
    assert "## Vulnerability Reproduction — 4-Phase Process" in bundle.spec.rendered_prompt
    assert FLAT_SUBAGENT_NOTE in bundle.spec.rendered_prompt


def _openhands_worker_overlay(*, enable_subagents: bool) -> dict:
    """Build an OpenHands worker overlay with optional native subagents."""
    return {
        "model": "gpt-5.3-codex",
        "tool": "openhands",
        "allowed_tools": ["file_editor", "glob", "grep"],
        "disallowed_tools": [],
        "timeout": 300,
        "max_iterations_per_run": 20,
        "tool_params": {
            "openhands": {
                "mcp_tools": ["shell_in_container"],
                "enable_subagents": enable_subagents,
            }
        },
    }


def test_flat_invariant_builder_openhands_without_subagents_omits_note(
    tmp_path: Path,
) -> None:
    """OpenHands without subagents must not receive ``FLAT_SUBAGENT_NOTE``.

    The legacy predicate keyed off ``"Task" not in worker.disallowed_tools``;
    an OpenHands configuration never lists Claude's Task tool, so it would wrongly get
    the note. The predicate must be tool-aware.
    """
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings
    from core.application.services.prompt.prompt_builder import FLAT_SUBAGENT_NOTE

    # Given: OpenHands with native subagents disabled and Task not disallowed.
    settings_path = _settings_with(
        tmp_path,
        worker=_openhands_worker_overlay(enable_subagents=False),
        orchestration={"mode": "flat"},
    )
    settings = Settings.from_yaml(settings_path)
    builder = _make_flat_invariant_builder(
        settings=settings, prompt_builder=_prompt_builder(), context_file=None
    )

    # When: the closure renders for a CVE.
    run_dir = tmp_path / "runs" / "without-subagents"
    run_dir.mkdir(parents=True)
    bundle = builder(task="t1", domain_context=_demo_cve_instance(), run_dir=run_dir)

    # Then: the pipeline is present but the subagent note is absent.
    assert "## Vulnerability Reproduction — 4-Phase Process" in bundle.spec.rendered_prompt
    assert FLAT_SUBAGENT_NOTE not in bundle.spec.rendered_prompt


def test_flat_invariant_builder_openhands_with_subagents_includes_note(
    tmp_path: Path,
) -> None:
    """OpenHands with native subagents receives ``FLAT_SUBAGENT_NOTE``."""
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings
    from core.application.services.prompt.prompt_builder import FLAT_SUBAGENT_NOTE

    # Given: OpenHands with subagent delegation enabled.
    settings_path = _settings_with(
        tmp_path,
        worker=_openhands_worker_overlay(enable_subagents=True),
        orchestration={"mode": "flat"},
    )
    settings = Settings.from_yaml(settings_path)
    builder = _make_flat_invariant_builder(
        settings=settings, prompt_builder=_prompt_builder(), context_file=None
    )

    # When: the closure renders for a CVE.
    run_dir = tmp_path / "runs" / "with-subagents"
    run_dir.mkdir(parents=True)
    bundle = builder(task="t1", domain_context=_demo_cve_instance(), run_dir=run_dir)

    # Then: the 4-phase pipeline AND the subagent note are present.
    assert "## Vulnerability Reproduction — 4-Phase Process" in bundle.spec.rendered_prompt
    assert FLAT_SUBAGENT_NOTE in bundle.spec.rendered_prompt


def test_flat_invariant_builder_omits_subagent_note_when_task_denied(
    tmp_path: Path,
) -> None:
    """The flat prompt omits ``FLAT_SUBAGENT_NOTE`` when Task is denied."""
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings
    from core.application.services.prompt.prompt_builder import FLAT_SUBAGENT_NOTE

    # Given: the Task tool is denied.
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
    run_dir = tmp_path / "runs" / "task-denied"
    run_dir.mkdir(parents=True)
    bundle = builder(task="t1", domain_context=_demo_cve_instance(), run_dir=run_dir)

    # Then: the pipeline is present but the subagent note is absent.
    assert "## Vulnerability Reproduction — 4-Phase Process" in bundle.spec.rendered_prompt
    assert FLAT_SUBAGENT_NOTE not in bundle.spec.rendered_prompt


def test_flat_prompt_carries_no_tree_or_judge_vocabulary(tmp_path: Path) -> None:
    """The flat worker prompt = CVE + BEF pipeline + artifacts + task — nothing else.

    Tree-topology vocabulary (decompose/subtask/sibling/manager), judge framing,
    and the shared-code block are hierarchical-arm engineering; any of them in the
    flat prompt contaminates the baseline (cve.j2's decomposition guidance leaked
    into every recorded N run before it was gated on ``include_decomposition``).
    """
    from bootstrap.composition import _make_flat_invariant_builder
    from config.settings import Settings

    forbidden = (
        "decompos",  # decompose/decomposition — boss/manager vocabulary
        "subtask",
        "sibling",
        "peer worker",
        "judge",
        "provided_source_files",
        "your manager",
        "<context-update>",
    )

    for cell, enable_subagents in (("disabled", False), ("enabled", True)):
        # Given: OpenHands with native subagent delegation toggled by configuration.
        settings_path = _settings_with(
            tmp_path,
            worker=_openhands_worker_overlay(enable_subagents=enable_subagents),
            orchestration={"mode": "flat"},
        )
        settings = Settings.from_yaml(settings_path)
        builder = _make_flat_invariant_builder(
            settings=settings, prompt_builder=_prompt_builder(), context_file=None
        )
        run_dir = tmp_path / "runs" / f"vocab-{cell}"
        run_dir.mkdir(parents=True)

        # When: the closure renders for a CVE.
        prompt = builder(
            task="t1", domain_context=_demo_cve_instance(), run_dir=run_dir
        ).spec.rendered_prompt
        lowered = prompt.lower()

        # Then: the four sanctioned blocks are present and nothing forbidden is.
        hits = [term for term in forbidden if term in lowered]
        assert not hits, f"{cell} flat prompt contains forbidden vocabulary: {hits}"
        assert "<cve_instance>" in prompt
        assert "## Vulnerability Reproduction — 4-Phase Process" in prompt
        assert "model_patch.diff" in prompt
        assert "<task>" in prompt
