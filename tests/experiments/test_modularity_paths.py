"""Unit tests for file-touch extraction (correctness req #2/#3).

Content strings are copied verbatim from real B1 events probed during design.
"""

from __future__ import annotations

from experiments.shared.scripts.analysis.modularity.activity import Activity, activity_of_sdk
from experiments.shared.scripts.analysis.modularity.paths import (
    extract_command_paths,
    extract_touches,
    normalize_path,
    zone_of,
)
from experiments.shared.scripts.db.models import EventRow


def _tool_use(tool: str, content: str) -> EventRow:
    return EventRow("e", "node-1", 1, "ThoughtCaptured",
                    {"output_type": "tool_use", "tool_name": tool, "content": content},
                    "2026-05-16T00:00:00Z", {})


def _probe(probe_type: str, summary: str) -> EventRow:
    return EventRow("e", "node-1", 1, "ProbeCompleted",
                    {"probe_type": probe_type, "result_summary": summary},
                    "2026-05-16T00:00:00Z", {})


def test_read_uses_input_file_path():
    t = extract_touches(_tool_use("Read", 'Reading: /testcase/repro.sh\nInput: {"file_path": "/testcase/repro.sh"}'))
    assert len(t) == 1
    assert (t[0].op, t[0].path, t[0].confidence, t[0].zone) == ("read", "/testcase/repro.sh", "high", "testcase")


def test_write_falls_back_to_prefix_when_no_file_path():
    # Write's Input often carries only "content"; the path is in the prefix.
    t = extract_touches(_tool_use("Write", 'Writing: /testcase/exploit_analysis.txt\nInput: {"content": "report"}'))
    assert (t[0].op, t[0].path, t[0].confidence) == ("write", "/testcase/exploit_analysis.txt", "high")


def test_edit_source_file():
    t = extract_touches(_tool_use(
        "Edit",
        'Editing: /src/libplist/tools/plistutil.c\nInput: {"file_path": "/src/libplist/tools/plistutil.c", "new_string": "x"}'))
    assert (t[0].op, t[0].path, t[0].zone) == ("edit", "/src/libplist/tools/plistutil.c", "src")


def test_bash_extracts_only_zoned_subpaths_low_confidence():
    # Conservative: only files *within* a known zone are captured (never the
    # bare zone dir, never /tmp); shell file effects are LOW confidence.
    t = extract_touches(_tool_use("Bash", 'Running: build\nInput: {"command": "gcc -o /work/bin/app /src/a.c; rm -rf /tmp/junk", "description": "d"}'))
    paths = {(x.op, x.path, x.confidence) for x in t}
    assert ("exec", "/work/bin/app", "low") in paths
    assert ("exec", "/src/a.c", "low") in paths
    assert all(p[1] != "/tmp/junk" for p in paths)


def test_bash_with_no_zoned_path_is_none():
    t = extract_touches(_tool_use("Bash", 'Running: env\nInput: {"command": "echo hello", "description": "d"}'))
    assert (t[0].op, t[0].path, t[0].confidence) == ("exec", None, "none")


def test_grep_search_root_medium():
    t = extract_touches(_tool_use("Grep", 'Searching content: pat\nInput: {"path": "/src/x/y.c", "pattern": "p"}'))
    assert (t[0].op, t[0].path, t[0].confidence) == ("search", "/src/x/y.c", "med")


def test_probe_read_symbol_header():
    t = extract_touches(_probe("read_symbol", "[/src/libplist/tools/plistutil.c:107-173 — main]\n107: int main("))
    assert (t[0].channel, t[0].op, t[0].path, t[0].confidence) == ("recon", "read", "/src/libplist/tools/plistutil.c", "high")


def test_probe_read_file_header_else_none():
    hit = extract_touches(_probe("read_file", "[/src/libplist/src/bplist.c lines 1-80 of 1294]\n1: /*"))
    assert hit[0].path == "/src/libplist/src/bplist.c"
    miss = extract_touches(_probe("read_file", "/*\n * plistutil.c raw content with no header"))
    assert (miss[0].path, miss[0].confidence) == (None, "none")


def test_probe_search_codebase_relative_paths_normalized():
    summary = ("src/libplist/tools/plistutil.c:2: * plistutil.c\n"
               "src/libplist/test/plist_cmp.c:128:        plist_from_bin(")
    t = extract_touches(_probe("search_codebase", summary))
    paths = {x.path for x in t}
    assert paths == {"/src/libplist/tools/plistutil.c", "/src/libplist/test/plist_cmp.c"}
    assert all(x.confidence == "med" and x.zone == "src" for x in t)


def test_probe_structure_has_no_path():
    t = extract_touches(_probe("get_file_structure", "├── cython/\n│   ├── Makefile.am"))
    assert (t[0].op, t[0].path, t[0].confidence) == ("structure", None, "none")


def test_normalize_and_zone():
    assert normalize_path("src/a/b.c") == "/src/a/b.c"  # relative -> container-absolute
    assert normalize_path("/work/bin/x") == "/work/bin/x"
    assert zone_of("/testcase/repro.sh") == "testcase"
    assert zone_of("/etc/passwd") == "other"
    assert zone_of(None) is None


def test_extract_command_paths_conservative():
    cmd = "cd /src/proj && gcc -o /work/bin/app a.c; rm -rf /tmp/junk"
    assert set(extract_command_paths(cmd)) == {"/src/proj", "/work/bin/app"}  # /tmp ignored


def test_activity_edit_src_is_patch_else_artifact():
    assert activity_of_sdk("Edit", None, "src") is Activity.PATCH
    assert activity_of_sdk("Write", None, "testcase") is Activity.WRITE_ARTIFACT
    assert activity_of_sdk("Bash", "make -j4", None) is Activity.BUILD
    assert activity_of_sdk("Bash", "valgrind ./a", None) is Activity.TEST
