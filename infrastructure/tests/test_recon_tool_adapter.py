from pathlib import Path

import pytest

from core.ports.domain_plugin_port import WorkspacePathAlias
from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter


@pytest.mark.asyncio
async def test_recon_adapter_maps_container_aliases_to_run_workspace(
    tmp_path: Path,
) -> None:
    # Given:
    source_dir = tmp_path / "src"
    testcase_dir = tmp_path / "testcase"
    work_dir = tmp_path / "work"
    (source_dir / "demo").mkdir(parents=True)
    testcase_dir.mkdir()
    work_dir.mkdir()
    source_file = source_dir / "demo" / "vuln.c"
    source_file.write_text("int main(void) { return 0; }\n")
    (testcase_dir / "base_commit_hash").write_text("abc123\n")

    adapter = ReconToolAdapter(str(tmp_path))
    adapter.set_path_aliases(
        (
            WorkspacePathAlias("/src", str(source_dir)),
            WorkspacePathAlias("/testcase", str(testcase_dir)),
            WorkspacePathAlias("/work", str(work_dir)),
        )
    )

    # When:
    aliased_read = await adapter.read_file("/src/demo/vuln.c")
    host_read = await adapter.read_file(str(source_file))
    testcase_listing = await adapter.list_directory("/testcase")
    found_files = await adapter.find_file("*.c", "/src")

    # Then:
    assert "int main" in aliased_read
    assert "int main" in host_read
    assert "base_commit_hash" in testcase_listing
    assert "src/demo/vuln.c" in found_files


@pytest.mark.asyncio
async def test_recon_adapter_rejects_absolute_paths_outside_workspace(
    tmp_path: Path,
) -> None:
    # Given:
    adapter = ReconToolAdapter(str(tmp_path))

    # When:
    result = await adapter.execute_tool("read_file", {"path": "/etc/passwd"})

    # Then:
    assert result == "Error: Path escapes working directory: /etc/passwd"


@pytest.mark.asyncio
async def test_recon_adapter_rejects_alias_escape_outside_workspace(
    tmp_path: Path,
) -> None:
    # Given:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    adapter = ReconToolAdapter(str(tmp_path))
    adapter.set_path_aliases((WorkspacePathAlias("/src", str(source_dir)),))

    # When:
    result = await adapter.execute_tool(
        "read_file",
        {"path": "/src/../../outside.txt"},
    )

    # Then:
    assert result == "Error: Path escapes working directory: /src/../../outside.txt"
