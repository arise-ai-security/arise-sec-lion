"""Tests for workspace path alias mapping."""

from pathlib import Path

import pytest

from core.ports.domain_plugin_port import WorkspacePathAlias
from infrastructure.adapters.workspace_paths import WorkspacePathMapper


def test_workspace_path_mapper_maps_virtual_paths_to_host_mirrors(
    tmp_path: Path,
) -> None:
    """Virtual workspace paths resolve to their run-scoped host mirrors."""

    # Given: A mapper with canonical container aliases.
    mapper = WorkspacePathMapper(
        (
            WorkspacePathAlias("/src", str(tmp_path / "src")),
            WorkspacePathAlias("/testcase", str(tmp_path / "testcase")),
            WorkspacePathAlias("/work", str(tmp_path / "work")),
        )
    )

    # When: Mapping virtual paths used by agents.
    source_path = mapper.map_virtual_to_host("/src/demo/vuln.c")
    testcase_path = mapper.map_virtual_to_host("/testcase/poc")
    unrelated_path = mapper.map_virtual_to_host("/tmp/file")

    # Then: Known aliases point at host mirrors and unknown paths are unchanged.
    assert source_path == str(tmp_path / "src" / "demo" / "vuln.c")
    assert testcase_path == str(tmp_path / "testcase" / "poc")
    assert unrelated_path == "/tmp/file"


def test_workspace_path_mapper_resolves_relative_host_mirrors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative run roots still produce absolute host paths for local tools."""

    # Given: A mapper built from a run path relative to the process cwd.
    monkeypatch.chdir(tmp_path)
    mapper = WorkspacePathMapper(
        (
            WorkspacePathAlias("/testcase", "runs/demo/testcase"),
        )
    )

    # When: Mapping a container path used by an agent.
    testcase_path = mapper.map_virtual_to_host("/testcase/base_commit_hash")

    # Then: The host-side tool receives an absolute path, not a cwd-relative path.
    assert testcase_path == str(tmp_path / "runs" / "demo" / "testcase" / "base_commit_hash")


def test_workspace_path_mapper_rewrites_host_paths_back_to_virtual(
    tmp_path: Path,
) -> None:
    """Host mirror paths in observations are hidden behind virtual aliases."""

    # Given: A nested alias where longest host prefixes must win.
    host_source_dir = tmp_path / "src"
    host_work_dir = host_source_dir / "demo"
    mapper = WorkspacePathMapper(
        (
            WorkspacePathAlias("/src", str(host_source_dir)),
            WorkspacePathAlias("/src/demo", str(host_work_dir)),
            WorkspacePathAlias("/testcase", str(tmp_path / "testcase")),
        )
    )
    observation = {
        "path": str(host_work_dir / "main.c"),
        "matches": [
            f"{host_source_dir / 'lib.c'}:1:int f(void)",
            str(tmp_path / "testcase" / "poc"),
        ],
    }

    # When: Sanitizing an observation payload.
    sanitized = mapper.sanitize_host_paths(observation)

    # Then: The model-facing payload contains only virtual paths.
    assert sanitized == {
        "path": "/src/demo/main.c",
        "matches": [
            "/src/lib.c:1:int f(void)",
            "/testcase/poc",
        ],
    }


def test_workspace_path_mapper_rejects_relative_virtual_alias(
    tmp_path: Path,
) -> None:
    """Virtual alias prefixes must be absolute paths."""

    # Given: A relative virtual alias.
    aliases = (WorkspacePathAlias("src", str(tmp_path / "src")),)

    # When/Then: Constructing the mapper fails fast.
    with pytest.raises(ValueError, match="Path alias must be absolute"):
        WorkspacePathMapper(aliases)
