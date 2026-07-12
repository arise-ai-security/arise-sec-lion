"""Tests for study-level confirmatory human-audit enrollment."""

from experiments.shared.evaluation.audit_queue import (
    AuditReason,
    JudgedTask,
    enroll_audit_queue,
)


def _unanimous(task_id: str, *, verdict: bool) -> JudgedTask:
    return JudgedTask(task_id=task_id, unanimous=True, provisional_verdict=verdict)


def _split(task_id: str, *, verdict: bool) -> JudgedTask:
    return JudgedTask(task_id=task_id, unanimous=False, provisional_verdict=verdict)


def test_non_unanimous_always_enrolled_regardless_of_seed() -> None:
    # Given: Two 2-to-1 split tasks among many unanimous ones
    results = [_split("split-a", verdict=True), _split("split-b", verdict=False)]
    results += [_unanimous(f"u{i}", verdict=i % 2 == 0) for i in range(40)]

    # When: The queue is enrolled under two different seeds
    seed_a = enroll_audit_queue(results, seed=1)
    seed_b = enroll_audit_queue(results, seed=999)

    # Then: Both split tasks are enrolled under either seed with the split reason
    for queue in (seed_a, seed_b):
        split = {e.task_id for e in queue if e.reason is AuditReason.NON_UNANIMOUS}
        assert split == {"split-a", "split-b"}


def test_judge_error_is_enrolled() -> None:
    # Given: A task whose judges errored (fail-closed)
    results = [
        JudgedTask(task_id="broken", unanimous=False, provisional_verdict=False, judge_error=True),
        _unanimous("ok", verdict=True),
    ]

    # When: The queue is enrolled
    queue = enroll_audit_queue(results, seed=0)

    # Then: The errored task is routed to review as a judge error
    reasons = {e.task_id: e.reason for e in queue}
    assert reasons["broken"] is AuditReason.JUDGE_ERROR


def test_stratified_unanimous_sample_is_about_ten_percent_and_deterministic() -> None:
    # Given: 100 unanimous tasks, evenly split across the pass/fail strata
    results = [_unanimous(f"t{i:03d}", verdict=i % 2 == 0) for i in range(100)]

    # When: Enrolled twice with the same seed and once with a different seed
    first = enroll_audit_queue(results, seed=5)
    same = enroll_audit_queue(results, seed=5)
    other = enroll_audit_queue(results, seed=6)

    sampled_first = {e.task_id for e in first}
    sampled_same = {e.task_id for e in same}
    sampled_other = {e.task_id for e in other}

    # Then: Exactly ~10% of unanimous tasks are sampled (5 per 50-task stratum)
    assert len(sampled_first) == 10
    assert all(e.reason is AuditReason.STRATIFIED_UNANIMOUS_SAMPLE for e in first)
    # And: The sample is reproducible for a seed and shifts with the seed
    assert sampled_first == sampled_same
    assert sampled_first != sampled_other


def test_both_verdict_strata_are_represented() -> None:
    # Given: A lopsided cohort - many passes, few fails
    results = [_unanimous(f"pass{i:02d}", verdict=True) for i in range(30)]
    results += [_unanimous(f"fail{i:02d}", verdict=False) for i in range(10)]

    # When: The stratified sample is drawn
    queue = enroll_audit_queue(results, seed=11)
    sampled = {e.task_id for e in queue}

    # Then: The small fail stratum is still audited (stratification, not global top-k)
    assert any(task_id.startswith("fail") for task_id in sampled)
    assert any(task_id.startswith("pass") for task_id in sampled)


def test_queue_is_sorted_and_deterministic() -> None:
    # Given: A mixed cohort
    results = [
        _split("z-split", verdict=True),
        _unanimous("a-unan", verdict=True),
        _unanimous("b-unan", verdict=False),
    ]

    # When: Enrolled twice with the same seed
    first = enroll_audit_queue(results, seed=2)
    second = enroll_audit_queue(results, seed=2)

    # Then: The queue is a stable, reproducible ordering
    assert first == second
    assert list(first) == sorted(first, key=lambda e: (e.reason.value, e.task_id))


def test_audit_rate_zero_samples_no_unanimous() -> None:
    # Given: Unanimous tasks plus one split
    results = [_unanimous(f"u{i}", verdict=True) for i in range(20)]
    results.append(_split("split", verdict=False))

    # When: The audit rate is zero
    queue = enroll_audit_queue(results, seed=3, audit_rate=0.0)

    # Then: Only the mandatory non-unanimous task is enrolled
    assert [e.task_id for e in queue] == ["split"]
