"""Generate a per-run verdict (mechanical, LLM-judged, or strict) and print it as JSON.

Thin runnable wrapper over :func:`experiments.shared.evaluation.criteria.evaluate_run`:
loads a run (events from Postgres + ``runs/<run_id>/`` artifacts), resolves the
host-side :class:`CveOracle` from the manifest ``task`` slug (fed to the oracle-aware
judges), and writes the verdict JSON to stdout.

Default is mechanical-only (no LLM call). ``--judge`` runs the four semantic LLM
judges (CVE-reproduced, binary-genuine, patch-root-cause, execution-provenance) via
the boundary-clean :class:`~experiments.shared.evaluation.judge.LLMJudge`; ``--strict``
additionally demands the blocking judges pass, so "success" means VERIFIED (the CVE
was really reproduced + the patch fixes the root cause + the verdict is backed by the
event-sourced transcript), not merely contract-complete.

Usage::

    python -m experiments.shared.scripts.evaluate_run <run_id> [--no-oracle] [--gold-patch]
    python -m experiments.shared.scripts.evaluate_run <run_id> --strict [--model gpt-5.5-2026-04-23]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from experiments.shared.evaluation.criteria import evaluate_run
from experiments.shared.evaluation.judge import (
    DEFAULT_JUDGE_MODEL,
    DEFAULT_REASONING_EFFORT,
    LLMJudge,
)
from experiments.shared.evaluation.loading import load_run


logger = logging.getLogger(__name__)


async def run_verdict(
    run_id: str,
    *,
    with_oracle: bool = True,
    include_gold_patch: bool = False,
    judge: LLMJudge | None = None,
    strict: bool = False,
) -> dict[str, object]:
    """Load the run and compute its structured verdict (mechanical, judged, or strict)."""
    run_data = await load_run(
        run_id, with_oracle=with_oracle, include_gold_patch=include_gold_patch
    )
    if with_oracle and run_data.cve is None:
        logger.warning(
            "No CVE oracle resolved for run %s (task=%r)",
            run_id,
            run_data.manifest.get("task"),
        )
    return evaluate_run(run_data, judge=judge, strict=strict)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the contract-only (mechanical) per-run verdict as JSON."
    )
    parser.add_argument("run_id", help="The run/root (BOSS) aggregate id.")
    parser.add_argument(
        "--no-oracle",
        action="store_true",
        help="Skip CVE-oracle resolution (mechanical-only verdict).",
    )
    parser.add_argument(
        "--gold-patch",
        action="store_true",
        help="Carry the host-side gold patch on the oracle (patch judge only).",
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="Run the four semantic LLM judges (not mechanical-only).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Strict verdict: success also requires the blocking judges pass (implies --judge).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_JUDGE_MODEL,
        help=f"Judge model (default: {DEFAULT_JUDGE_MODEL}).",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=DEFAULT_REASONING_EFFORT,
        help=f"Judge reasoning effort (default: {DEFAULT_REASONING_EFFORT}).",
    )
    args = parser.parse_args(argv)

    judge = (
        LLMJudge(model=args.model, reasoning_effort=args.reasoning_effort)
        if (args.judge or args.strict)
        else None
    )
    verdict = asyncio.run(
        run_verdict(
            args.run_id,
            with_oracle=not args.no_oracle,
            include_gold_patch=args.gold_patch,
            judge=judge,
            strict=args.strict,
        )
    )
    sys.stdout.write(json.dumps(verdict, indent=2, sort_keys=True) + "\n")
    if judge is not None:
        sys.stderr.write(
            f"judge: model={judge.model} calls={judge.calls} errors={judge.errors} "
            f"usage={judge.usage} cost_usd={round(judge.cost_usd, 4)}\n"
        )
    return 0 if verdict.get("overall") else 1


if __name__ == "__main__":
    raise SystemExit(main())
