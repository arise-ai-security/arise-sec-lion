"""Regression tests for atomic events.jsonl writes (audit N-1).

A killed harness mid-write must NOT leave a truncated events.jsonl that the
reader silently treats as a shortened event sequence. The writer uses
tmp+fsync+replace so the target file is either fully replaced or left
untouched.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from core.domain.events.events import RunCompleted
from experiments.shared.scripts import project_events


logger = logging.getLogger(__name__)


def _fake_events(n: int) -> list[Any]:
    return [
        RunCompleted(
            aggregate_id=uuid4(),
            sequence_number=i,
            status="completed",
            duration_seconds=float(i),
            total_agents=1,
            completed_agents=1,
            failed_agents=0,
        )
        for i in range(n)
    ]


def test_atomic_write_replaces_target_in_one_step(tmp_path: Path) -> None:
    # Given: a target path with previous contents
    target = tmp_path / "events.jsonl"
    target.write_text("OLD CONTENT", encoding="utf-8")

    # When: a fresh write succeeds
    project_events._atomic_write_events_jsonl(target, _fake_events(3))

    # Then: the file has exactly the new contents, no .tmp leftover
    lines = target.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    for i, line in enumerate(lines):
        assert json.loads(line)["duration_seconds"] == float(i)
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"leftover tmp files: {tmp_files}"


def test_atomic_write_cleans_up_tmp_on_failure(tmp_path: Path) -> None:
    # Given: a writer that raises mid-write
    target = tmp_path / "events.jsonl"
    target.write_text("PREVIOUS", encoding="utf-8")

    # Patch model_dump to raise so the write loop fails after creating the tmp file.
    # The atomic writer now uses model_dump(mode="json") + an event_type wrap
    # (audit N-5), so model_dump is the right hook point.
    def _boom(self: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated mid-write kill")

    with patch.object(RunCompleted, "model_dump", _boom):
        # When: the write is attempted
        with pytest.raises(RuntimeError, match="simulated mid-write kill"):
            project_events._atomic_write_events_jsonl(target, _fake_events(3))

    # Then: the target retains its previous content and no .tmp leftover
    assert target.read_text(encoding="utf-8") == "PREVIOUS"
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"leftover tmp files: {tmp_files}"
