"""Classify a node action into a coarse activity type (Claim 2 fingerprint).

The reliable, deterministic Claim-2 signal is recon activity derived from
``probe_type`` (structure / search / read) — these are what "analyze the
project structure" maps to. SDK/Bash activities are noisier and reuse the
team's :func:`classify_bash_command`; they are reported as secondary.
"""

from __future__ import annotations

import enum
import re

from experiments.shared.scripts.analysis.text.bash_classifier import (
    BashSubtype,
    classify_bash_command,
)


class Activity(str, enum.Enum):
    RECON_STRUCTURE = "recon_structure"
    RECON_SEARCH = "recon_search"
    RECON_READ = "recon_read"
    BUILD = "build"
    TEST = "test"
    PATCH = "patch"
    WRITE_ARTIFACT = "write_artifact"
    EXEC_OTHER = "exec_other"
    OTHER = "other"


_PROBE_ACTIVITY = {
    "get_file_structure": Activity.RECON_STRUCTURE,
    "get_symbols_overview": Activity.RECON_STRUCTURE,
    "search_codebase": Activity.RECON_SEARCH,
    "read_file": Activity.RECON_READ,
    "read_symbol": Activity.RECON_READ,
}

# Applying/diffing a patch (not merely naming a *.patch/.diff file).
_PATCH_RE = re.compile(r"(git\s+apply|patch\s+-p|\bgit\s+diff\b)", re.IGNORECASE)
_TEST_RE = re.compile(r"\b(valgrind|asan|ubsan|sanitiz|gdb|addr2line)\b", re.IGNORECASE)
_RUNPOC_RE = re.compile(r"(repro\.sh|/work/bin/|\./poc|\./exploit)", re.IGNORECASE)
_RECON_CMD_RE = re.compile(
    r"\b(ls|cat|find|grep|file|nm|objdump|readelf|strings|head|tail|wc|xxd|tree|stat)\b",
    re.IGNORECASE,
)

_READ_TOOLS = {"Read", "read_file"}
_SEARCH_TOOLS = {"Grep", "Glob"}
_EDIT_WRITE_TOOLS = {"Edit", "MultiEdit", "str_replace_editor", "Write", "write_file"}
_SHELL_TOOLS = {"Bash", "mcp__security_tools__shell_in_container", "execute_command", "shell_execute"}


def activity_of_probe(probe_type: str) -> Activity:
    return _PROBE_ACTIVITY.get(probe_type, Activity.RECON_READ)


def activity_of_command(command: str) -> Activity:
    if not command:
        return Activity.EXEC_OTHER
    if _PATCH_RE.search(command):
        return Activity.PATCH
    if _TEST_RE.search(command) or _RUNPOC_RE.search(command):
        return Activity.TEST
    subtype = classify_bash_command(command)
    if subtype is BashSubtype.BUILD:
        return Activity.BUILD
    if subtype is BashSubtype.TEST_EXEC:
        return Activity.TEST
    if subtype is BashSubtype.GIT:
        return Activity.PATCH
    if _RECON_CMD_RE.search(command):
        return Activity.RECON_READ
    return Activity.EXEC_OTHER


def activity_of_sdk(tool: str, command: str | None, zone: str | None) -> Activity:
    if tool in _READ_TOOLS:
        return Activity.RECON_READ
    if tool in _SEARCH_TOOLS:
        return Activity.RECON_SEARCH
    if tool in _EDIT_WRITE_TOOLS:
        return Activity.PATCH if zone == "src" else Activity.WRITE_ARTIFACT
    if tool == "mcp__security_tools__valgrind_run":
        return Activity.TEST
    if tool in _SHELL_TOOLS:
        return activity_of_command(command or "")
    return Activity.OTHER
