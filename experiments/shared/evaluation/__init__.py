"""Offline evaluation functions for a single experiment run.

Reads the event stream from the Postgres event store (source of truth) and the
on-disk ``runs/<run_id>/`` artifacts, and computes cost, prompt, tool-usage,
cache, artifact, and success-criteria metrics.

Two public families:

- :mod:`experiments.shared.evaluation.bef` — hierarchical BEF runs (B3, B4).
- :mod:`experiments.shared.evaluation.linear` — flat runs.

All metric functions are pure over a loaded :class:`RunData`; only
:func:`load_run` touches the database. See
``docs/superpowers/specs/2026-06-13-experiment-evaluation-functions-design.md``.
"""

from __future__ import annotations

from experiments.shared.evaluation.export import dump_runs_sql, render_events_dump
from experiments.shared.evaluation.loading import RUNS_DIR, detect_topology, load_run
from experiments.shared.evaluation.models import (
    ArtifactRef,
    ArtifactsBySubtree,
    BefPhase,
    CostByRole,
    CountBreakdown,
    CveOracle,
    RateBreakdown,
    RunData,
    ToolCall,
    ToolCategory,
    WorkerPrompt,
    WorkerPrompts,
)


__all__ = [
    "RUNS_DIR",
    "ArtifactRef",
    "ArtifactsBySubtree",
    "BefPhase",
    "CostByRole",
    "CountBreakdown",
    "CveOracle",
    "RateBreakdown",
    "RunData",
    "ToolCall",
    "ToolCategory",
    "WorkerPrompt",
    "WorkerPrompts",
    "detect_topology",
    "dump_runs_sql",
    "load_run",
    "render_events_dump",
]
