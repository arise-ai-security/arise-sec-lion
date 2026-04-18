"""Post-run anti-cheat audit for experiment runs.

Walks events.jsonl for a run and flags tool invocations matching the
forbidden patterns defined in the domain briefing:
  - git checkout of any non-base commit
  - git log --all, git branch -a (upstream enumeration)
  - curl / wget to external URLs
  - WebFetch tool to external URLs
  - References to known patch-source domains (github.com/*/commit/*, nvd.nist.gov)

Writes audit.json alongside events.jsonl. Runs are NOT auto-excluded; Pillar B
reports results with and without violating runs separately.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


# External-URL detection patterns. Order matters: specific patterns first,
# generic catch-all last.
EXTERNAL_URL_PATTERNS = [
    re.compile(r"https?://github\.com/[^/]+/[^/]+/commit/", re.IGNORECASE),
    re.compile(r"https?://nvd\.nist\.gov/", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?cve\.(?:mitre|org)", re.IGNORECASE),
    # generic external-host catch-all (any non-localhost http(s) URL)
    re.compile(r"https?://[^/\s]+", re.IGNORECASE),
]

# Internal/loopback hosts that don't count as external
INTERNAL_HOSTS = ("localhost", "127.", "0.0.0.0", "::1")  # noqa: S104

GIT_CHECKOUT_SHA_RE = re.compile(
    r"\bgit\s+checkout\s+(?P<sha>[A-Fa-f0-9]{7,40}|\S+)",
    re.IGNORECASE,
)
GIT_LOG_ALL_RE = re.compile(r"\bgit\s+log\b[^\n]*--all\b", re.IGNORECASE)
GIT_BRANCH_ALL_RE = re.compile(r"\bgit\s+branch\b[^\n]*-a\b", re.IGNORECASE)
CURL_WGET_HTTP_RE = re.compile(r"\b(?:curl|wget)\b[^\n]*https?://", re.IGNORECASE)


def audit_run(events_path: Path, *, base_commit: str) -> dict[str, Any]:
    """Scan events.jsonl; return {violated, violations, violation_count} report dict."""
    violations: list[dict[str, Any]] = []
    if not events_path.exists():
        return {"violated": False, "violations": [], "violation_count": 0}

    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "Skipping malformed events.jsonl line (first 200 chars): %s",
                line[:200],
            )
            continue
        if ev.get("event_type") != "tool_use":
            continue
        payload = ev.get("payload") or {}
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input") or {}

        if tool_name == "Bash":
            command = str(tool_input.get("command", ""))
            _check_bash(command, base_commit, ev, violations)
        elif tool_name == "WebFetch":
            url = str(tool_input.get("url", ""))
            if _is_external_url(url):
                violations.append(
                    {
                        "type": "webfetch_external",
                        "event_id": ev.get("event_id"),
                        "tool_input": tool_input,
                    }
                )

    return {
        "violated": bool(violations),
        "violations": violations,
        "violation_count": len(violations),
    }


def _check_bash(
    command: str,
    base_commit: str,
    ev: dict[str, Any],
    violations: list[dict[str, Any]],
) -> None:
    m = GIT_CHECKOUT_SHA_RE.search(command)
    if m:
        sha = m.group("sha").lower().rstrip(".")
        if not _is_base_commit(sha, base_commit):
            violations.append(
                {
                    "type": "git_checkout",
                    "event_id": ev.get("event_id"),
                    "matched": command.strip(),
                    "checked_out_sha": sha,
                }
            )
    if GIT_LOG_ALL_RE.search(command):
        violations.append(
            {
                "type": "git_log_all",
                "event_id": ev.get("event_id"),
                "matched": command.strip(),
            }
        )
    if GIT_BRANCH_ALL_RE.search(command):
        violations.append(
            {
                "type": "git_branch_all",
                "event_id": ev.get("event_id"),
                "matched": command.strip(),
            }
        )
    # external URL fetches via curl / wget
    if CURL_WGET_HTTP_RE.search(command):
        urls = re.findall(r"https?://\S+", command)
        for url in urls:
            if _is_external_url(url):
                violations.append(
                    {
                        "type": "external_url_fetch",
                        "event_id": ev.get("event_id"),
                        "url": url.strip(),
                        "tool": "curl_or_wget",
                    }
                )


def _is_external_url(url: str) -> bool:
    """True if the URL points to a non-loopback host."""
    if any(tok in url for tok in INTERNAL_HOSTS):
        return False
    return any(pat.search(url) for pat in EXTERNAL_URL_PATTERNS)


def _is_base_commit(sha: str, base_commit: str) -> bool:
    """Match short or full SHAs against the configured base commit."""
    base = base_commit.lower()
    sha_lower = sha.lower()
    # Full match or prefix match (short SHAs are valid prefixes of the base SHA)
    return base.startswith(sha_lower) or sha_lower.startswith(base[: len(sha_lower)])


def main() -> None:
    """CLI wrapper around :func:`audit_run`; reads events.jsonl, writes audit.json."""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True, type=Path)
    ap.add_argument("--base-commit", required=True)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    report = audit_run(args.events, base_commit=args.base_commit)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
