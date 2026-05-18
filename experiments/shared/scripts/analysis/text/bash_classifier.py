"""Bash command subtype classifier (design doc §10).

Regex-based, first-match-wins dispatch over the Bash command string.
Priority is fixed by the order of ``_BASH_PATTERNS``:

    web_via_shell > recon > exploit > security_scan > build > test_exec > git > other_shell

Overlapping commands (e.g. ``curl ... | gcc -``) increment ONLY the
highest-priority bucket. The conservative bias documented in §14.6
applies: ``https?://`` matches anywhere in the command, including
comments and flag descriptions, so URL-bearing strings are treated as
``web_via_shell`` even when no HTTP fetch actually runs.
"""

from __future__ import annotations

import enum
import re
from typing import Final

from experiments.shared.scripts.analysis.text.forbidden_web import BASH_WEB_RE


class BashSubtype(str, enum.Enum):
    WEB_VIA_SHELL = "web_via_shell"
    RECON = "recon"
    EXPLOIT = "exploit"
    SECURITY_SCAN = "security_scan"
    BUILD = "build"
    TEST_EXEC = "test_exec"
    GIT = "git"
    OTHER_SHELL = "other_shell"


# Ordered list — first match wins. Case-insensitive for all entries
# (including ``GIT`` whose anchor only constrains leading whitespace, not
# the case of the literal ``git``). ``WEB_VIA_SHELL`` reuses
# ``forbidden_web.BASH_WEB_RE`` as the single source of truth so the
# subtype classifier and the forbidden-web detector cannot diverge.
_BASH_PATTERNS: Final[list[tuple[BashSubtype, re.Pattern[str]]]] = [
    (BashSubtype.WEB_VIA_SHELL, BASH_WEB_RE),
    (
        BashSubtype.RECON,
        re.compile(r"\b(nmap|masscan|gobuster|dirb|nikto|whatweb|hydra)\b", re.IGNORECASE),
    ),
    (
        BashSubtype.EXPLOIT,
        re.compile(r"\b(sqlmap|msfconsole|metasploit|pwntools)\b", re.IGNORECASE),
    ),
    (
        BashSubtype.SECURITY_SCAN,
        re.compile(
            r"\b(bandit|semgrep|codeql|trivy|grype|osv-scanner|safety)\b",
            re.IGNORECASE,
        ),
    ),
    (
        BashSubtype.BUILD,
        re.compile(
            r"\b(gcc|g\+\+|clang|clang\+\+|make|cmake|cargo\s+build|go\s+build|meson|ninja)\b",
            re.IGNORECASE,
        ),
    ),
    (
        BashSubtype.TEST_EXEC,
        re.compile(r"\b(pytest|gtest|afl-fuzz|libfuzzer)\b|\./fuzz", re.IGNORECASE),
    ),
    (
        BashSubtype.GIT,
        re.compile(r"^\s*git\b", re.IGNORECASE),
    ),
]


def classify_bash_command(cmd: str) -> BashSubtype:
    """First-match-wins over the ordered pattern list. Returns OTHER_SHELL on no match."""
    if not cmd:
        return BashSubtype.OTHER_SHELL
    for subtype, rx in _BASH_PATTERNS:
        if rx.search(cmd):
            return subtype
    return BashSubtype.OTHER_SHELL
