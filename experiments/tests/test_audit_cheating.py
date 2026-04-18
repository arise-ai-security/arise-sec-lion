"""Tests for the anti-cheat audit script."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from experiments.audit_cheating import audit_run


if TYPE_CHECKING:
    from pathlib import Path


def _write_events(tmp_path: Path, events: list[dict]) -> Path:
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events))
    return p


def test_audit_flags_git_checkout_of_non_base_commit(tmp_path: Path) -> None:
    """git checkout of a non-base SHA is flagged as a violation."""
    # Given: a tool_use event for `git checkout <non-base>`
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:00+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 0,
            "payload": {
                "tool_name": "Bash",
                "tool_input": {"command": "git checkout deadbeef"},
                "call_id": "tu_1",
            },
        }
    ]
    events_path = _write_events(tmp_path, events)
    base_commit = "a" * 40

    # When: audited
    report = audit_run(events_path, base_commit=base_commit)

    # Then: violated=True, 1 violation of type git_checkout
    assert report["violated"] is True
    assert any(v["type"] == "git_checkout" for v in report["violations"])


def test_audit_allows_git_checkout_of_base_commit(tmp_path: Path) -> None:
    """git checkout of the configured base commit is allowed."""
    # Given: a tool_use for `git checkout <base>`
    base_commit = "a" * 40
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:00+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 0,
            "payload": {
                "tool_name": "Bash",
                "tool_input": {"command": f"git checkout {base_commit}"},
                "call_id": "t",
            },
        }
    ]

    # When: audited
    report = audit_run(_write_events(tmp_path, events), base_commit=base_commit)

    # Then: not violated
    assert report["violated"] is False


def test_audit_flags_curl_external_url(tmp_path: Path) -> None:
    """curl to an external URL (e.g., github.com commit URL) is flagged."""
    # Given: a tool_use for `curl <github commit URL>`
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:00+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 0,
            "payload": {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "curl https://github.com/foo/bar/commit/abc"
                },
                "call_id": "t",
            },
        }
    ]

    # When: audited
    report = audit_run(_write_events(tmp_path, events), base_commit="a" * 40)

    # Then: external_url_fetch violation present
    assert report["violated"] is True
    assert any(v["type"] == "external_url_fetch" for v in report["violations"])


def test_audit_flags_webfetch_external(tmp_path: Path) -> None:
    """WebFetch with an external URL is flagged."""
    # Given: a WebFetch tool_use to NVD
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:00+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 0,
            "payload": {
                "tool_name": "WebFetch",
                "tool_input": {
                    "url": "https://nvd.nist.gov/vuln/detail/CVE-2022-32414"
                },
                "call_id": "t",
            },
        }
    ]

    # When: audited
    report = audit_run(_write_events(tmp_path, events), base_commit="a" * 40)

    # Then: webfetch_external violation present
    assert report["violated"] is True
    assert any(v["type"] == "webfetch_external" for v in report["violations"])


def test_audit_clean_run_has_no_violations(tmp_path: Path) -> None:
    """A run with only secb invocations produces no violations."""
    # Given: a tool_use for `./secb build`
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:00+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 0,
            "payload": {
                "tool_name": "Bash",
                "tool_input": {"command": "./secb build"},
                "call_id": "t",
            },
        }
    ]

    # When: audited
    report = audit_run(_write_events(tmp_path, events), base_commit="a" * 40)

    # Then: not violated, empty violation list
    assert report["violated"] is False
    assert report["violations"] == []


def test_audit_flags_git_log_all(tmp_path: Path) -> None:
    """`git log --all` enumerates upstream history and is flagged."""
    # Given: a tool_use event for `git log --all --oneline`
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:00+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 0,
            "payload": {
                "tool_name": "Bash",
                "tool_input": {"command": "git log --all --oneline"},
                "call_id": "t",
            },
        }
    ]

    # When: audited
    report = audit_run(_write_events(tmp_path, events), base_commit="a" * 40)

    # Then: git_log_all violation present
    assert report["violated"] is True
    assert any(v["type"] == "git_log_all" for v in report["violations"])


def test_audit_flags_git_branch_a(tmp_path: Path) -> None:
    """`git branch -a` enumerates remote branches and is flagged."""
    # Given: a tool_use for `git branch -a`
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:00+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 0,
            "payload": {
                "tool_name": "Bash",
                "tool_input": {"command": "git branch -a"},
                "call_id": "t",
            },
        }
    ]

    # When: audited
    report = audit_run(_write_events(tmp_path, events), base_commit="a" * 40)

    # Then: git_branch_all violation present
    assert report["violated"] is True
    assert any(v["type"] == "git_branch_all" for v in report["violations"])


def test_audit_aggregates_multiple_violations(tmp_path: Path) -> None:
    """Multiple distinct violations across events are all collected."""
    # Given: three events with three different violations
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:00+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 0,
            "payload": {
                "tool_name": "Bash",
                "tool_input": {"command": "git checkout deadbeef"},
                "call_id": "t1",
            },
        },
        {
            "event_id": "e2",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:01+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 1,
            "payload": {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "wget https://example.com/patch.diff"
                },
                "call_id": "t2",
            },
        },
        {
            "event_id": "e3",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:02+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 2,
            "payload": {
                "tool_name": "WebFetch",
                "tool_input": {"url": "https://github.com/foo/bar/commit/abc"},
                "call_id": "t3",
            },
        },
    ]

    # When: audited
    report = audit_run(_write_events(tmp_path, events), base_commit="a" * 40)

    # Then: all three violation types appear; total count is 3
    assert report["violated"] is True
    assert report["violation_count"] == 3
    types = {v["type"] for v in report["violations"]}
    assert types == {"git_checkout", "external_url_fetch", "webfetch_external"}


def test_audit_missing_events_file_returns_clean_report(tmp_path: Path) -> None:
    """An events.jsonl that doesn't exist yields a clean (non-violated) report."""
    # Given: a non-existent events.jsonl path
    events_path = tmp_path / "events.jsonl"

    # When: audited
    report = audit_run(events_path, base_commit="a" * 40)

    # Then: clean report with zero violations
    assert report["violated"] is False
    assert report["violations"] == []
    assert report["violation_count"] == 0


def test_audit_empty_events_file_returns_clean_report(tmp_path: Path) -> None:
    """An empty events.jsonl yields a clean report."""
    # Given: an empty events.jsonl
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")

    # When: audited
    report = audit_run(events_path, base_commit="a" * 40)

    # Then: clean report
    assert report["violated"] is False
    assert report["violation_count"] == 0


def test_audit_short_sha_prefix_of_base_is_allowed(tmp_path: Path) -> None:
    """A short-SHA checkout that is a prefix of the base commit is allowed."""
    # Given: base commit and a 7-char prefix checkout
    base_commit = "abcdef0123456789" + "0" * 24  # 40 chars
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:00+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 0,
            "payload": {
                "tool_name": "Bash",
                "tool_input": {"command": f"git checkout {base_commit[:7]}"},
                "call_id": "t",
            },
        }
    ]

    # When: audited
    report = audit_run(_write_events(tmp_path, events), base_commit=base_commit)

    # Then: not violated
    assert report["violated"] is False


def test_audit_webfetch_to_localhost_is_allowed(tmp_path: Path) -> None:
    """WebFetch to localhost is not considered an external URL."""
    # Given: a WebFetch to localhost
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:00+00:00",
            "event_type": "tool_use",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 0,
            "payload": {
                "tool_name": "WebFetch",
                "tool_input": {"url": "http://localhost:8080/status"},
                "call_id": "t",
            },
        }
    ]

    # When: audited
    report = audit_run(_write_events(tmp_path, events), base_commit="a" * 40)

    # Then: not violated
    assert report["violated"] is False


def test_audit_skips_malformed_json_lines(tmp_path: Path) -> None:
    """A malformed JSON line is skipped without crashing the audit."""
    # Given: a file with a malformed line followed by a valid tool_use
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "this is not json\n"
        + json.dumps(
            {
                "event_id": "e2",
                "run_id": "r",
                "occurred_at": "2026-04-18T00:00:00+00:00",
                "event_type": "tool_use",
                "source": "flat_cli",
                "role": "FLAT",
                "depth": 0,
                "sequence_number": 0,
                "payload": {
                    "tool_name": "Bash",
                    "tool_input": {"command": "./secb build"},
                    "call_id": "t",
                },
            }
        )
    )

    # When: audited
    report = audit_run(events_path, base_commit="a" * 40)

    # Then: does not crash; malformed line ignored; clean otherwise
    assert report["violated"] is False
    assert report["violation_count"] == 0


def test_audit_ignores_non_tool_use_events(tmp_path: Path) -> None:
    """Non-tool_use events (e.g., prompt_sent) are ignored."""
    # Given: a prompt_sent event that mentions forbidden commands in its text
    events = [
        {
            "event_id": "e1",
            "run_id": "r",
            "occurred_at": "2026-04-18T00:00:00+00:00",
            "event_type": "prompt_sent",
            "source": "flat_cli",
            "role": "FLAT",
            "depth": 0,
            "sequence_number": 0,
            "payload": {
                "prompt_text": "Do not run git checkout deadbeef or wget https://example.com",
                "prompt_type": "worker_execution",
                "model": "claude-opus-4-7",
            },
        }
    ]

    # When: audited
    report = audit_run(_write_events(tmp_path, events), base_commit="a" * 40)

    # Then: prompt text is not scanned; no violations
    assert report["violated"] is False
