from pathlib import Path

from infrastructure.adapters.worker.shared import ContainerSessionContext


def _session() -> ContainerSessionContext:
    workspace_root = Path("/workspace/run")
    return ContainerSessionContext(
        container_id="abc123def456",
        container_name="domain-worker",
        image="tools:demo.issue-2024-0001",
        workspace_root=workspace_root,
        host_source_dir=workspace_root / "src",
        host_testcase_dir=workspace_root / "testcase",
        host_work_dir=workspace_root / "src" / "demo",
        container_source_dir="/src",
        container_testcase_dir="/testcase",
        container_working_directory="/src/demo",
        helper_script=workspace_root / "secb-exec",
    )


def test_container_session_maps_container_paths_to_host() -> None:
    session = _session()

    translated = session.translate_tool_input(
        "Read",
        {"file_path": "/src/demo/main.c"},
    )

    assert translated["file_path"] == "/workspace/run/src/demo/main.c"


def test_container_session_maps_work_artifacts_to_host() -> None:
    session = _session()

    translated = session.translate_tool_input(
        "Read",
        {"file_path": "/work/bin/demo"},
    )

    assert translated["file_path"] == "/workspace/run/work/bin/demo"


def test_container_session_wraps_shell_commands() -> None:
    session = _session()

    wrapped = session.wrap_shell_command("/src/build.sh")

    assert "docker exec -i" in wrapped
    assert "abc123def456" in wrapped
    assert "/src/demo" in wrapped
    assert "/src/build.sh" in wrapped


def test_container_session_prefix_mentions_shell_in_container_for_manual_shell() -> None:
    # E.10: the manual-shell branch must route the agent at the MCP
    # ``shell_in_container`` tool, NOT at a host-side ``./secb-exec`` binary.
    session = _session()

    prompt = session.apply_task_prefix("Do the task", auto_shell=False)

    assert "/workspace/run/src" in prompt
    assert "/workspace/run/testcase" in prompt
    assert "/workspace/run/work" in prompt
    assert "shell_in_container" in prompt
    assert "./secb-exec" not in prompt


def test_container_session_prefix_mentions_shell_in_container_for_auto_shell() -> None:
    # E.10: the auto-shell branch (claude_sdk / google_adk path) still
    # advertises the MCP tool as preferred, even though Bash tool calls
    # are auto-routed into the container by the permission hook.
    session = _session()

    prompt = session.apply_task_prefix("Do the task", auto_shell=True)

    assert "shell_in_container" in prompt
    assert "./secb-exec" not in prompt
