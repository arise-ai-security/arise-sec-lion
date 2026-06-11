"""Workspace listing whitelist and entry cap.

The <workspace> block exists for artifact discovery (worker-produced files,
chiefly testcase/). Legacy mode lists everything except src/ — an out-of-tree
CMake build under work/ alone contributed ~650 object/Makefile entries to every
prompt turn. ``workspace_listing_dirs`` whitelists top-level dirs;
``workspace_listing_max_entries`` caps the listing with an overflow summary.
Defaults preserve legacy bytes.
"""

from pathlib import Path

from config import BossConfig, ManagerConfig
from core.application.execution_service import AgentExecutionService, ServiceConfig


def _service(tmp_path: Path, **config_overrides: object) -> AgentExecutionService:
    service = object.__new__(AgentExecutionService)
    service._config = ServiceConfig(
        max_retries=3,
        poll_interval=0.5,
        output_directory=str(tmp_path),
        default_worker_tool="openhands",
        boss_config=BossConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        manager_config=ManagerConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
        **config_overrides,  # type: ignore[arg-type]
    )
    service._working_directory = tmp_path
    return service


def _populate_workspace(tmp_path: Path) -> None:
    (tmp_path / "testcase").mkdir()
    (tmp_path / "testcase" / "repro.sh").write_text("#!/bin/sh\n")
    (tmp_path / "testcase" / "asan.log").write_text("crash\n")
    (tmp_path / "work" / "demo-build" / "CMakeFiles").mkdir(parents=True)
    (tmp_path / "work" / "demo-build" / "CMakeFiles" / "demo.o").write_text("obj")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.c").write_text("int main(){}\n")
    (tmp_path / "secb-exec").write_text("#!/bin/sh\n")


def test_legacy_listing_skips_only_src(tmp_path: Path) -> None:
    # Given: a workspace with testcase artifacts, a build tree, sources, a helper
    _populate_workspace(tmp_path)
    service = _service(tmp_path)

    # When
    listing = service._get_workspace_context()

    # Then: everything except src/ is listed (build noise included — legacy)
    assert listing is not None
    assert "testcase/repro.sh" in listing
    assert "work/demo-build/CMakeFiles/demo.o" in listing
    assert "secb-exec" in listing
    assert "src/main.c" not in listing


def test_whitelist_lists_only_included_dirs(tmp_path: Path) -> None:
    # Given
    _populate_workspace(tmp_path)
    service = _service(tmp_path, workspace_listing_dirs=("testcase",))

    # When
    listing = service._get_workspace_context()

    # Then: artifact discovery without build-tree or helper noise
    assert listing is not None
    assert "testcase/repro.sh" in listing
    assert "testcase/asan.log" in listing
    assert "demo.o" not in listing
    assert "secb-exec" not in listing
    assert "src/main.c" not in listing


def test_entry_cap_summarizes_overflow(tmp_path: Path) -> None:
    # Given: three testcase files and a cap of one entry
    _populate_workspace(tmp_path)
    (tmp_path / "testcase" / "gdb.txt").write_text("bt\n")
    service = _service(
        tmp_path,
        workspace_listing_dirs=("testcase",),
        workspace_listing_max_entries=1,
    )

    # When
    listing = service._get_workspace_context()

    # Then: one entry plus an overflow summary, never a silent truncation
    assert listing is not None
    lines = listing.splitlines()
    assert len(lines) == 2
    assert lines[1] == "- … (+2 more files)"


def test_whitelist_with_no_matching_files_returns_none(tmp_path: Path) -> None:
    # Given: a workspace whose whitelisted dir does not exist
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "scratch.txt").write_text("x")
    service = _service(tmp_path, workspace_listing_dirs=("testcase",))

    # When / Then: no block at all (text_if drops it), not an empty listing
    assert service._get_workspace_context() is None
