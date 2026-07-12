"""Study-level human-audit enrollment for the confirmatory judge cohort.

Complements the per-run gate in :mod:`experiments.shared.evaluation.semantic_gate`
(which decides audit for ONE run) by enrolling the whole cohort's audit queue:
every non-unanimous (or judge-errored) task, plus a deterministic,
seed-reproducible stratified 10% sample of the unanimous tasks.

Unlike the per-run gate's seedless ``task_id`` hash, the cohort sample takes an
explicit ``seed`` so the confirmatory study can pre-register exactly which
unanimous cases were audited and reproduce the queue byte-for-byte.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Sequence


class AuditReason(str, Enum):
    """Why a task was routed into the human-audit queue."""

    JUDGE_ERROR = "judge_error"
    NON_UNANIMOUS = "non_unanimous"
    STRATIFIED_UNANIMOUS_SAMPLE = "stratified_unanimous_sample"


@dataclass(frozen=True, slots=True)
class JudgedTask:
    """One task's blinded multi-call judge summary, as fed to the audit queue.

    Built from a :class:`~experiments.shared.evaluation.semantic_gate.SemanticGateResult`
    at the call site (``task_id``, ``result.unanimous``, ``result.provisional_verdict``,
    ``result.judge_error``); kept as a minimal value object so enrollment is
    testable without constructing a full gate result.
    """

    task_id: str
    unanimous: bool
    provisional_verdict: bool
    judge_error: bool = False


@dataclass(frozen=True, slots=True)
class AuditEnrollment:
    """One task enrolled into the human-audit queue, with the routing reason."""

    task_id: str
    reason: AuditReason


def enroll_audit_queue(
    results: Sequence[JudgedTask],
    *,
    seed: int,
    audit_rate: float = 0.10,
) -> tuple[AuditEnrollment, ...]:
    """Enroll all non-unanimous/errored tasks plus a stratified unanimous sample.

    Deterministic given ``seed``: the unanimous sample is drawn per provisional-
    verdict stratum by ranking each stratum on a seed-salted stable hash and
    taking the top ``ceil(audit_rate * stratum_size)``, so both the pass and fail
    strata are always represented and the same seed reproduces the same queue.
    Non-unanimous and judge-errored tasks are enrolled unconditionally.

    Args:
        results: Per-task blinded judge summaries.
        seed: Explicit seed making the unanimous sample reproducible.
        audit_rate: Fraction of each unanimous stratum to audit (default 10%).

    Returns:
        The audit queue as a stable, reason-then-task-id sorted tuple.
    """
    if not 0.0 <= audit_rate <= 1.0:
        raise ValueError("audit_rate must be between 0 and 1")

    enrolled: list[AuditEnrollment] = []
    unanimous_by_stratum: dict[bool, list[JudgedTask]] = defaultdict(list)
    for task in results:
        if task.judge_error:
            enrolled.append(AuditEnrollment(task.task_id, AuditReason.JUDGE_ERROR))
        elif not task.unanimous:
            enrolled.append(AuditEnrollment(task.task_id, AuditReason.NON_UNANIMOUS))
        else:
            unanimous_by_stratum[task.provisional_verdict].append(task)

    for stratum in unanimous_by_stratum.values():
        enrolled.extend(
            AuditEnrollment(task.task_id, AuditReason.STRATIFIED_UNANIMOUS_SAMPLE)
            for task in _sample_stratum(stratum, seed=seed, audit_rate=audit_rate)
        )

    enrolled.sort(key=lambda enrollment: (enrollment.reason.value, enrollment.task_id))
    return tuple(enrolled)


def _sample_stratum(
    stratum: list[JudgedTask], *, seed: int, audit_rate: float
) -> list[JudgedTask]:
    """Top ``ceil(audit_rate * n)`` tasks of a stratum by seed-salted stable hash."""
    if not stratum or audit_rate == 0.0:
        return []
    # Round before ceil so float error (e.g. 0.1 * 100 == 10.000000000000002)
    # does not silently over-sample by one.
    count = math.ceil(round(audit_rate * len(stratum), 6))
    ranked = sorted(stratum, key=lambda task: _stable_bucket(task.task_id, seed))
    return ranked[:count]


def _stable_bucket(task_id: str, seed: int) -> int:
    """A seed-salted, stable 64-bit bucket for one task id."""
    digest = hashlib.sha256(f"{seed}:{task_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")
