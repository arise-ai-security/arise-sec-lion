"""Scripted quantitative metrics for one run.

The report pipeline must copy numeric values from scripts, not recompute them
in prose. This module is the per-run primitive: given a run_id it can query
the Postgres event store, or read a projected events.jsonl file when a study
is being regenerated offline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from config import Settings
from core.domain.events.events import DomainEvent
from core.query.projections import ProjectionPipelineBuilder
from infrastructure.adapters.postgres_event_store import PostgresEventStore


logger = logging.getLogger(__name__)


MetricValue = int | float | str | dict[str, int]


async def query_run_metrics(
    run_id: UUID,
    *,
    config_path: Path | None = None,
) -> dict[str, MetricValue]:
    """Query Postgres for ``run_id`` and return deterministic numeric metrics."""
    settings = Settings.from_yaml(config_path) if config_path else Settings.load()
    store = PostgresEventStore(settings.database.connection_string)
    await store.connect()
    try:
        events = await ProjectionPipelineBuilder(store).build().execute_events(run_id)
    finally:
        await store.disconnect()
    return metrics_from_events(events)


def metrics_from_events_jsonl(path: Path) -> dict[str, MetricValue]:
    """Read projected JSONL events and return the same metric schema.

    Raises ``ValueError`` on a malformed non-empty line (audit N-1). The
    writer is atomic (`project_events._atomic_write_events_jsonl`) so a
    half-written file is impossible under normal operation; if a line is
    malformed here, something corrupted the file out-of-band and silently
    summing what's left would produce a wrong row. Use strict UTF-8 for
    the same reason — `errors="replace"` would mask non-text corruption.
    """
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return empty_metrics()
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path}:{lineno}: malformed JSON line — refusing to silently "
                "skip; events.jsonl is written atomically by project_events"
            ) from exc
        if isinstance(row, dict):
            rows.append(row)
    return metrics_from_events(rows)


def empty_metrics() -> dict[str, MetricValue]:
    # `run_duration_seconds = -1.0` is the audit-N-7 sentinel for "no
    # RunCompleted event observed". Loud (negative in CSV) vs the old
    # silent `0.0` indistinguishable from a real zero-second run.
    # `tokens_cache_read` / `tokens_cache_write` are surfaced as their
    # own columns (audit N-3) so cross-cell token comparisons stay
    # symmetric between Claude-SDK and OpenHands worker adapters.
    return {
        "event_count": 0,
        "prompt_sent_count": 0,
        "tool_call_count": 0,
        "probe_count": 0,
        "tool_result_count": 0,
        "thinking_event_count": 0,
        "thinking_chars": 0,
        "worker_output_event_count": 0,
        "worker_cost_event_count": 0,
        "tokens_prompt": 0,
        "tokens_completion": 0,
        "tokens_total": 0,
        "tokens_reasoning": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
        "llm_cost_usd": 0.0,
        "worker_cost_usd": 0.0,
        "total_cost_usd": 0.0,
        "run_duration_seconds": -1.0,
        "run_completed_count": 0,
        # `judge_score = -1` is the sentinel for "no verification-judge event
        # observed" (parallels run_duration_seconds). 0–100 means the judge
        # stage ran and produced that score. Sourced from VerificationPassed
        # or VerificationFailed(failed_stage="judge").
        "judge_score": -1,
        "tool_calls_by_type": {},
    }


def metrics_from_events(  # noqa: PLR0915
    events: list[DomainEvent] | list[dict[str, Any]],
) -> dict[str, MetricValue]:
    """Compute the canonical metric set from typed or raw event objects."""
    metrics = empty_metrics()
    tool_calls_by_type: dict[str, int] = {}

    for event in events:
        _incr(metrics, "event_count")
        event_type = _event_type(event)

        if event_type == "PromptSent":
            _incr(metrics, "prompt_sent_count")

        elif event_type == "ThoughtCaptured":
            output_type = str(_get(event, "output_type") or "output")
            content = str(_get(event, "content") or "")
            _incr(metrics, "worker_output_event_count")
            if output_type == "tool_use":
                _incr(metrics, "tool_call_count")
                structured = _get(event, "tool_name")
                tool_name = (
                    structured.strip()
                    if isinstance(structured, str) and structured.strip()
                    else "unknown"
                )
                tool_calls_by_type[tool_name] = tool_calls_by_type.get(tool_name, 0) + 1
            elif output_type == "tool_result":
                _incr(metrics, "tool_result_count")
            elif output_type == "thinking":
                _incr(metrics, "thinking_event_count")
                _incr(metrics, "thinking_chars", len(content))

        elif event_type == "ProbeStarted":
            _incr(metrics, "probe_count")
            probe = str(_get(event, "probe_type") or "probe")
            tool_calls_by_type[probe] = tool_calls_by_type.get(probe, 0) + 1

        elif event_type == "ProbeCompleted":
            _incr(metrics, "tool_result_count")

        elif event_type == "VerificationPassed":
            metrics["judge_score"] = _int(_get(event, "score"))

        elif event_type == "VerificationFailed":
            # Only the judge stage produces a meaningful score; other stages
            # (structural / deterministic / execution) emit score=0 by default.
            if str(_get(event, "failed_stage") or "") == "judge":
                metrics["judge_score"] = _int(_get(event, "score"))

        elif event_type == "TokensConsumed":
            prompt = _int(_get(event, "prompt_tokens"))
            completion = _int(_get(event, "completion_tokens"))
            total = _int(_get(event, "total_tokens"))
            cost = _float(_get(event, "cost_usd"))
            _incr(metrics, "tokens_prompt", prompt)
            _incr(metrics, "tokens_completion", completion)
            _incr(metrics, "tokens_total", total)
            _add_float(metrics, "llm_cost_usd", cost)

        elif event_type == "WorkerCostRecorded":
            # Audit N-3: sum breakdown buckets so Claude-SDK and OpenHands
            # cells compare symmetrically (Claude-SDK historically set
            # `tokens = prompt+completion` only).
            _incr(metrics, "worker_cost_event_count")
            prompt = _int(_get(event, "prompt_tokens"))
            completion = _int(_get(event, "completion_tokens"))
            cache_read = _int(_get(event, "cache_read_tokens"))
            cache_write = _int(_get(event, "cache_write_tokens"))
            reasoning = _int(_get(event, "reasoning_tokens"))
            total = prompt + completion + cache_read + cache_write + reasoning
            cost = _float(_get(event, "cost_usd"))
            _incr(metrics, "tokens_prompt", prompt)
            _incr(metrics, "tokens_completion", completion)
            _incr(metrics, "tokens_reasoning", reasoning)
            _incr(metrics, "tokens_cache_read", cache_read)
            _incr(metrics, "tokens_cache_write", cache_write)
            _incr(metrics, "tokens_total", total)
            _add_float(metrics, "worker_cost_usd", cost)

        elif event_type == "RunCompleted":
            # Audit N-7: track count + assign duration. Post-loop fixup
            # sets the sentinel when count==0 and warns on count>1.
            _incr(metrics, "run_completed_count")
            metrics["run_duration_seconds"] = _float(_get(event, "duration_seconds"))

    # Audit N-12: round components first, then sum the rounded values, so
    # the invariant `total == round(llm + worker, 6)` holds after rounding.
    metrics["llm_cost_usd"] = round(_float(metrics.get("llm_cost_usd")), 6)
    metrics["worker_cost_usd"] = round(_float(metrics.get("worker_cost_usd")), 6)
    metrics["total_cost_usd"] = round(
        _float(metrics.get("llm_cost_usd")) + _float(metrics.get("worker_cost_usd")),
        6,
    )
    # Audit N-7: emit a distinct sentinel when no RunCompleted was observed
    # (silently reporting 0.0 was indistinguishable from a real zero-second
    # run). Last-write-wins is logged so duplicate RunCompleted is loud.
    run_completed_count = _int(metrics.get("run_completed_count"))
    if run_completed_count == 0:
        metrics["run_duration_seconds"] = -1.0
    else:
        if run_completed_count > 1:
            logger.warning(
                "metrics_from_events: %d RunCompleted events observed; "
                "last-write-wins on run_duration_seconds",
                run_completed_count,
            )
        metrics["run_duration_seconds"] = round(
            _float(metrics.get("run_duration_seconds")),
            3,
        )
    metrics["tool_calls_by_type"] = dict(sorted(tool_calls_by_type.items()))
    return metrics


def _incr(metrics: dict[str, MetricValue], key: str, amount: int = 1) -> None:
    metrics[key] = _int(metrics.get(key)) + amount


def _add_float(metrics: dict[str, MetricValue], key: str, amount: float) -> None:
    metrics[key] = _float(metrics.get(key)) + amount


def _event_type(event: DomainEvent | dict[str, Any]) -> str:
    """Return the event class name, preferring the typed instance over the
    on-disk ``event_type`` discriminator (audit N-5).
    """
    if isinstance(event, DomainEvent):
        return type(event).__name__
    explicit = event.get("event_type")
    if isinstance(explicit, str) and explicit:
        return explicit
    return "Unknown"


def _get(event: DomainEvent | dict[str, Any], field: str) -> Any:
    if isinstance(event, DomainEvent):
        return getattr(event, field, None)
    return event.get(field)


def _int(value: Any) -> int:
    # Audit N-8: reject non-finite floats before `int(...)`. `int(float("inf"))`
    # raises OverflowError; the pre-fix code caught only TypeError/ValueError,
    # so a `cost_usd: Infinity` line in events.jsonl would propagate.
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        logger.warning("dropped non-finite int input: %r", value)
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _float(value: Any) -> float:
    # Audit N-8: `float("nan")` and `float("inf")` both parse cleanly; one
    # bad cost_usd value would otherwise propagate via `_add_float` and
    # poison every rollup (`nan + anything == nan`).
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(result):
        logger.warning("dropped non-finite float input: %r", value)
        return 0.0
    return result


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_metrics")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-id", type=UUID, help="Run id to query from Postgres")
    source.add_argument("--events-jsonl", type=Path, help="Projected events.jsonl to read")
    parser.add_argument("--config", type=Path, help="Config path for Postgres lookup")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.events_jsonl:
        metrics = metrics_from_events_jsonl(args.events_jsonl)
    else:
        metrics = asyncio.run(query_run_metrics(args.run_id, config_path=args.config))
    json.dump(metrics, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
