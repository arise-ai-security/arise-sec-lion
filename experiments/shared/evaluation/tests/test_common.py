"""Tests for the pure helpers in common.py."""

from uuid import uuid4

from experiments.shared.evaluation import common
from experiments.shared.evaluation.models import BefPhase, ToolCategory
from experiments.shared.evaluation.tests.builders import (
    RunBuilder,
    file_editor,
    glob,
    grep,
    mcp_other,
    shell,
    write_files,
)


def test_bracket_prefix_extracts_label() -> None:
    """The leading [Bracket] label is extracted; digits and missing brackets handled."""
    # Given/When/Then
    assert common.bracket_prefix("[Builder] do the build") == "Builder"
    assert common.bracket_prefix("  [PoC-Researcher] x") == "PoC-Researcher"
    assert common.bracket_prefix("[Build-Setup-2] y") == "Build-Setup-2"
    # And: no bracket / empty
    assert common.bracket_prefix("no bracket here") is None
    assert common.bracket_prefix(None) is None


def test_classify_phase_phases_and_leaves_only() -> None:
    """Exact phase names and finer leaf roles classify; loose keywords do NOT."""
    # Then: exact phase names
    assert common.classify_phase("Builder") is BefPhase.BUILDER
    assert common.classify_phase("Reporter") is BefPhase.REPORTER
    # And: finer leaf roles map to their phase
    assert common.classify_phase("PoC-Researcher") is BefPhase.EXPLOITER
    assert common.classify_phase("Root-Cause-Analyst") is BefPhase.FIXER
    # And: loose keyword text is NOT guessed (real phase nodes use exact brackets)
    assert common.classify_phase("apply the patch") is None
    assert common.classify_phase("totally-unrelated") is None
    assert common.classify_phase(None) is None


def test_build_role_map_uppercases_and_defaults() -> None:
    """Roles are uppercased; unknown agents resolve to UNKNOWN."""
    # Given
    builder = RunBuilder()
    boss = builder.boss()
    worker = builder.agent("worker", boss, "[Builder] x")
    # When
    role_map = common.build_role_map(builder.events)
    # Then
    assert role_map[boss] == "BOSS"
    assert role_map[worker] == "WORKER"
    assert common.role_key(role_map, uuid4()) == "UNKNOWN"


def test_role_map_resolves_pending_to_determined_role() -> None:
    """Pending agents resolve to their assessed role; the boss stays boss."""
    # Given: boss → a pending agent assessed as manager and one assessed as worker
    builder = RunBuilder()
    boss = builder.boss()
    manager = builder.assessed_agent("manager", boss, "[Builder] build")
    worker = builder.assessed_agent("worker", manager, "[Build-Setup] setup")
    # When
    role_map = common.build_role_map(builder.events)
    # Then
    assert role_map[boss] == "BOSS"
    assert role_map[manager] == "MANAGER"
    assert role_map[worker] == "WORKER"


def test_role_map_pins_boss_even_when_executed_as_worker() -> None:
    """The flat (N1/N2) root is created as boss and runs as a worker — stays BOSS."""
    # Given: a single flat agent created as boss but executing as a worker
    builder = RunBuilder()
    boss = builder.boss()
    builder.exec_started(boss, "worker")
    # When/Then
    assert common.build_role_map(builder.events)[boss] == "BOSS"


def test_bef_phase_map_walks_to_phase_node() -> None:
    """A deep leaf inherits the phase of its direct-boss-child ancestor."""
    # Given: boss → [Exploiter] manager → [PoC-Researcher] worker
    builder = RunBuilder()
    boss = builder.boss()
    manager = builder.agent("manager", boss, "[Exploiter] exploit phase")
    worker = builder.agent("worker", manager, "[PoC-Researcher] research")
    # When
    phase_map = common.build_bef_phase_map(builder.events, builder.run_id)
    # Then
    assert phase_map[worker] is BefPhase.EXPLOITER
    assert phase_map[manager] is BefPhase.EXPLOITER
    assert phase_map[boss] is BefPhase.ORCHESTRATION


def test_bef_phase_map_b3_direct_child_is_phase() -> None:
    """In a flat (B3) tree the phase worker is itself the direct boss-child."""
    # Given: boss → [Fixer] worker (depth 1)
    builder = RunBuilder()
    boss = builder.boss()
    worker = builder.agent("worker", boss, "[Fixer] fix it")
    # When/Then
    phase_map = common.build_bef_phase_map(builder.events, builder.run_id)
    assert phase_map[worker] is BefPhase.FIXER


def test_iter_tool_calls_excludes_thoughts_and_results() -> None:
    """Only output_type==tool_use with a tool_name counts; thoughts/results excluded."""
    # Given
    builder = RunBuilder()
    boss = builder.boss()
    builder.tool(boss, file_editor("view"))
    builder.tool(boss, file_editor("create"))
    builder.tool(boss, shell())
    builder.thought(boss, "just thinking")  # tool_use, tool_name=None -> excluded
    builder.tool_result(boss, "some result")  # tool_result -> excluded
    # When
    calls = list(common.iter_tool_calls(builder.events))
    # Then
    assert [c.category for c in calls] == [
        ToolCategory.FILE_READ,
        ToolCategory.FILE_WRITE,
        ToolCategory.BASH,
    ]


def test_categorize_covers_every_config_tool() -> None:
    """Each config-defined tool surface maps to its category."""
    # Given
    builder = RunBuilder()
    boss = builder.boss()
    builder.tool(boss, file_editor("view"))
    builder.tool(boss, file_editor("str_replace"))
    builder.tool(boss, grep())
    builder.tool(boss, glob())
    builder.tool(boss, shell())
    builder.tool(boss, mcp_other())
    builder.finish(boss)
    # When
    cats = [c.category for c in common.iter_tool_calls(builder.events)]
    # Then
    assert cats == [
        ToolCategory.FILE_READ,
        ToolCategory.FILE_WRITE,
        ToolCategory.GREP,
        ToolCategory.GLOB,
        ToolCategory.BASH,
        ToolCategory.MCP_OTHER,
        ToolCategory.FINISH,
    ]


def test_security_tools_bucketed_separately_from_bash() -> None:
    """Shell calls that invoke valgrind/klee go to their own buckets, not Bash."""
    # Given: a valgrind invocation, a klee invocation, and a plain shell call
    builder = RunBuilder()
    boss = builder.boss()
    builder.tool(boss, shell("valgrind --tool=memcheck --leak-check=full ./poc"))
    builder.tool(boss, shell("klee --max-time=60 program.bc"))
    builder.tool(boss, shell("ls -la /testcase"))
    # When
    cats = [c.category for c in common.iter_tool_calls(builder.events)]
    # Then
    assert cats == [ToolCategory.VALGRIND, ToolCategory.KLEE, ToolCategory.BASH]


def test_security_tool_filename_mentions_are_not_invocations() -> None:
    """Reading/writing files named after the tools stays Bash, not the tool bucket."""
    # Given: commands that only mention the tools as filename components
    builder = RunBuilder()
    boss = builder.boss()
    builder.tool(boss, shell("cat /testcase/fix_valgrind.log"))
    builder.tool(boss, shell("tail -n 20 /testcase/valgrind_poc_memcheck.log"))
    builder.tool(boss, shell("ls /src/klee/lib"))
    # When
    cats = [c.category for c in common.iter_tool_calls(builder.events)]
    # Then
    assert cats == [ToolCategory.BASH, ToolCategory.BASH, ToolCategory.BASH]


def test_security_tool_excludes_argument_and_heredoc_contexts() -> None:
    """The tool name as an arg, in `which`, or in heredoc content is NOT an invocation."""
    # Given: install (arg), availability checks (arg), and a packages heredoc listing
    builder = RunBuilder()
    boss = builder.boss()
    builder.tool(boss, shell("apt-get install -y cppcheck valgrind gdb"))
    builder.tool(boss, shell("which valgrind || true; which klee || true"))
    heredoc = "cat > /testcase/packages.txt << 'EOF'\ncppcheck\nvalgrind\nklee\nEOF"
    builder.tool(boss, shell(heredoc))
    # When
    cats = [c.category for c in common.iter_tool_calls(builder.events)]
    # Then
    assert cats == [ToolCategory.BASH, ToolCategory.BASH, ToolCategory.BASH]


def test_security_tool_detected_with_env_prefix_and_full_path() -> None:
    """Env-var prefixes and full-path invocations still resolve to the tool bucket."""
    # Given
    builder = RunBuilder()
    boss = builder.boss()
    builder.tool(boss, shell("ASAN_OPTIONS=detect_leaks=0 valgrind --tool=memcheck ./poc"))
    builder.tool(boss, shell("/usr/bin/klee --max-time=60 program.bc"))
    # When
    cats = [c.category for c in common.iter_tool_calls(builder.events)]
    # Then
    assert cats == [ToolCategory.VALGRIND, ToolCategory.KLEE]


def test_shell_tool_call_command_is_cleanly_parsed() -> None:
    """A shell MCP call's command is recovered without surrounding repr quotes."""
    # Given: a shell command containing quotes/redirection
    builder = RunBuilder()
    boss = builder.boss()
    builder.tool(boss, shell("git diff > /testcase/x.diff"))
    # When
    call = next(common.iter_tool_calls(builder.events))
    # Then
    assert call.category is ToolCategory.BASH
    assert call.command == "git diff > /testcase/x.diff"  # no surrounding quotes


def test_is_counted_tool_call_excludes_finish() -> None:
    """The Finish control signal is not a counted tool call."""
    # Given
    builder = RunBuilder()
    boss = builder.boss()
    builder.tool(boss, shell())
    builder.finish(boss)
    # When
    calls = list(common.iter_tool_calls(builder.events))
    # Then
    assert [common.is_counted_tool_call(c) for c in calls] == [True, False]


def test_container_path_to_disk_maps_mounts(tmp_path) -> None:
    """Only /testcase and /src map to disk; other roots return None."""
    # Given/When/Then
    assert common.container_path_to_disk("/testcase/a.txt", tmp_path) == (
        tmp_path / "testcase" / "a.txt"
    )
    assert common.container_path_to_disk("/src/build.sh", tmp_path) == tmp_path / "src" / "build.sh"
    assert common.container_path_to_disk("/etc/passwd", tmp_path) is None
    assert common.container_path_to_disk("", tmp_path) is None
    # And: a non-mount path WITHOUT a leading slash (e.g. an ArtifactStored key)
    assert common.container_path_to_disk("outputs/analysis.json", tmp_path) is None


def test_is_vacuous_for_missing_and_empty(tmp_path) -> None:
    """Missing and zero-byte files are vacuous; non-empty files are not."""
    # Given
    run_dir = write_files(tmp_path, {"/testcase/data": b"x", "/testcase/empty": b""})
    # Then
    assert common.is_vacuous(None) is True
    assert common.is_vacuous(run_dir / "testcase" / "missing") is True
    assert common.is_vacuous(run_dir / "testcase" / "empty") is True
    assert common.is_vacuous(run_dir / "testcase" / "data") is False
    # And: a directory is not a non-vacuous file
    assert common.is_vacuous(run_dir / "testcase") is True


def test_cache_rate_definition() -> None:
    """Rate is read/prompt; undefined (None) when prompt tokens are zero."""
    # Then
    assert common.cache_rate(50, 100) == 0.5
    assert common.cache_rate(0, 100) == 0.0
    assert common.cache_rate(5, 0) is None
