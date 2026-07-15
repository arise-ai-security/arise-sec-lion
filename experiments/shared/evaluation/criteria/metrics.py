"""Criteria deliverable/artifact metrics: artifacts per subtree (#5) and
per-subtree deliverable success (#6).

#5 (:func:`artifacts_by_bef`) attributes each non-vacuous file written/edited on
disk to the BEF subtree whose agent wrote it. #6 (:func:`success_criteria_by_bef`)
reports, per subtree, three independent results: which canonical deliverables
exist, the declared success criteria, and the agents' self-reported outcomes.

The canonical SEC-bench deliverable contract is mirrored here (``_REQUIRED_FILES``
etc.) rather than imported from ``plugins/security`` so this package does not
violate the composition-root boundary. A drift guard in the tests asserts the
mirror stays in sync with the plugin source of truth.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from core.domain.events.events import (
    AgentCreated,
    ChildSpawned,
    SourceFileEdited,
    VerificationFailed,
    VerificationPassed,
    WorkCompleted,
    WorkFailed,
)
from experiments.shared.evaluation.common import (
    build_bef_phase_map,
    container_path_to_disk,
    events_of_type,
    is_vacuous,
    parse_uuid,
)
from experiments.shared.evaluation.models import (
    ArtifactRef,
    ArtifactsBySubtree,
    BefPhase,
)


if TYPE_CHECKING:
    from uuid import UUID

    from core.domain.events.events import DomainEvent
    from experiments.shared.evaluation.models import RunData

# Local evaluation mirror of the SEC-bench prompt contract. Keep this out of
# plugins/security imports so the evaluation package does not violate the
# composition-root boundary.
_REQUIRED_FILES: dict[str, tuple[str, ...]] = {
    "Builder": (
        "/testcase/base_commit_hash",
        "/src/build.sh",
        "/testcase/repo_changes.diff",
        "/testcase/binary_paths.txt",
    ),
    "Exploiter": (
        "/testcase/poc_path.txt",
        "/testcase/repro.sh",
    ),
    "Fixer": (
        "/testcase/root_cause_analysis.txt",
        "/testcase/patch_plan.json",
        "/testcase/model_patch.diff",
    ),
    "Reporter": ("/testcase/security_report.md",),
}

_VALIDATION_REQUIRED: dict[str, tuple[str, ...]] = {
    "Exploiter": ("/testcase/exploit_validation_results.txt",),
    "Fixer": ("/testcase/patch_validation_results.txt",),
}

_MAY_BE_EMPTY: frozenset[str] = frozenset({"/testcase/repo_changes.diff"})

_ROLE_PHASE: dict[str, BefPhase] = {
    "Build-Setup": BefPhase.BUILDER,
    "Build-Executor": BefPhase.BUILDER,
    "Build-Verifier": BefPhase.BUILDER,
    "PoC-Researcher": BefPhase.EXPLOITER,
    "Data-Flow-Analyst": BefPhase.EXPLOITER,
    "PoC-Tester": BefPhase.EXPLOITER,
    "Forward-Instrumentator": BefPhase.EXPLOITER,
    "Repro-Creator": BefPhase.EXPLOITER,
    "Exploit-Validator": BefPhase.EXPLOITER,
    "Root-Cause-Analyst": BefPhase.FIXER,
    "Candidate-Reviewer": BefPhase.FIXER,
    "Regression-Tester": BefPhase.FIXER,
    "Patch-Applier": BefPhase.FIXER,
    "Patch-Validator": BefPhase.FIXER,
    "Fix-Aggregator": BefPhase.FIXER,
    "Reporter": BefPhase.REPORTER,
}

_ROLE_DEPENDS_ON: dict[str, tuple[str, ...]] = {
    "Build-Executor": ("Build-Setup",),
    "Build-Verifier": ("Build-Executor",),
    "Forward-Instrumentator": ("PoC-Researcher",),
    "Exploit-Validator": ("Repro-Creator",),
    "Candidate-Reviewer": ("Root-Cause-Analyst",),
    "Patch-Applier": ("Root-Cause-Analyst",),
    "Patch-Validator": ("Patch-Applier",),
    "Fix-Aggregator": ("Patch-Validator",),
}

# Deliverables that are legitimately empty on a successful run (e.g. an empty
# repo_changes.diff means the build needed no source-repo edits) are in
# ``_MAY_BE_EMPTY``; for these the key-file check tests presence only.


# Canonical SEC-bench deliverables per phase (container paths). A trailing ``*``
# is a prefix glob within the directory.
# This is mirrored from the prompt contract without importing the security plugin.
def _phase_key_files(phase: str) -> tuple[str, ...]:
    return _REQUIRED_FILES[phase] + _VALIDATION_REQUIRED.get(phase, ())


KEY_FILES: dict[BefPhase, tuple[str, ...]] = {
    BefPhase.BUILDER: _phase_key_files("Builder"),
    BefPhase.EXPLOITER: _phase_key_files("Exploiter"),
    BefPhase.FIXER: _phase_key_files("Fixer"),
    BefPhase.REPORTER: _phase_key_files("Reporter"),
}

_BEF_SUBTREES = (BefPhase.BUILDER, BefPhase.EXPLOITER, BefPhase.FIXER, BefPhase.REPORTER)

# All canonical deliverables across phases (used by the linear family, where one
# agent owns every phase).
ALL_KEY_FILES: tuple[str, ...] = tuple(spec for specs in KEY_FILES.values() for spec in specs)


def _is_hierarchical(run_data: RunData) -> bool:
    """Whether this run used decomposition into explicit phase sub-agents.

    Prefers manifest metadata and falls back to event structure. Judge prompt
    construction uses this distinction; artifact requirements do not.
    """
    if run_data.manifest:
        cell = str(run_data.manifest.get("cell", "")).strip().upper()
        study = str(run_data.manifest.get("study_id", "")).strip().lower()
        if cell.startswith("B") or study.startswith("b"):
            return True
        if cell.startswith("N") or study.startswith("n"):
            return False
    has_children = any(isinstance(event, ChildSpawned) for event in run_data.events)
    distinct_aggregates = len({event.aggregate_id for event in run_data.events})
    return has_children or distinct_aggregates > 1


_ROLE_PREFIX = re.compile(r"^\s*\[([^\]]+)\]")


def _catalog_role_name(description: str) -> str | None:
    match = _ROLE_PREFIX.match(description)
    if match is None:
        return None
    name = match.group(1).strip()
    return name if name in _ROLE_PHASE else None


def _dependency_contract_by_bef(events: list[DomainEvent]) -> dict[BefPhase, dict[str, Any]]:
    """Check prompt-only SEC-bench role dependency compliance after the run.

    This does not enforce scheduling. It reports whether a manager's emitted
    `depends_on` edges matched the hard artifact dependencies declared in
    plugins/security/roles.py.
    """
    spawned_by_parent: dict[UUID, list[ChildSpawned]] = defaultdict(list)
    for event in events:
        if isinstance(event, ChildSpawned):
            spawned_by_parent[event.aggregate_id].append(event)

    violations: dict[BefPhase, list[str]] = defaultdict(list)
    for siblings in spawned_by_parent.values():
        ordered = sorted(siblings, key=lambda e: e.sibling_index)
        role_index: dict[str, int] = {}
        for event in ordered:
            name = _catalog_role_name(event.subtask.description)
            if name is not None:
                role_index.setdefault(name, event.sibling_index)

        for event in ordered:
            name = _catalog_role_name(event.subtask.description)
            if name is None:
                continue
            phase = _ROLE_PHASE[name]
            depends_on = set(event.subtask.depends_on)
            producers = _ROLE_DEPENDS_ON.get(name, ())
            if name == "Build-Verifier" and "Build-Executor" in role_index:
                producers = ("Build-Executor",)
            elif name == "Patch-Validator" and "Patch-Applier" in role_index:
                producers = ("Patch-Applier",)
            for producer in producers:
                producer_index = role_index.get(producer)
                if producer_index is None:
                    violations[phase].append(f"[{name}] missing producer role [{producer}]")
                    continue
                if producer_index not in depends_on:
                    violations[phase].append(
                        f"[{name}] missing depends_on edge to [{producer}] at "
                        f"sibling index {producer_index}"
                    )
                if producer_index >= event.sibling_index:
                    violations[phase].append(
                        f"[{name}] producer [{producer}] must appear before consumer"
                    )

    return {
        phase: {
            "ok": not violations.get(phase),
            "violations": list(violations.get(phase, [])),
        }
        for phase in _BEF_SUBTREES
    }


# ---------------------------------------------------------------------------
# #5 — artifacts per subtree
# ---------------------------------------------------------------------------


def _edited_paths(run_data: RunData) -> list[tuple[str, UUID]]:
    """All (container_path, author_agent_id) pairs from file-edit events.

    Only ``SourceFileEdited`` is used: it records edits to the ``/testcase`` and
    ``/src`` mounts. ``ArtifactStored`` is deliberately excluded — it persists
    shared-context blobs (keyed e.g. ``outputs/analysis.json``), not files on the
    testcase deliverable mount.
    """
    pairs: list[tuple[str, UUID]] = []
    for event in events_of_type(run_data.events, SourceFileEdited):
        author = parse_uuid(event.edited_by) or event.aggregate_id
        pairs.append((event.path, author))
    return pairs


def artifacts_by_bef(run_data: RunData) -> ArtifactsBySubtree:
    """Non-vacuous files written/edited by each BEF subtree's agents.

    Container paths are resolved to ``runs/<run_id>/``; missing or zero-byte files
    are excluded. A file touched within a subtree appears once for that subtree
    (a file touched by two subtrees appears under each).
    """
    phase_map = build_bef_phase_map(run_data.events, run_data.run_id)
    by_phase: dict[str, list[ArtifactRef]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()

    for path, author in _edited_paths(run_data):
        disk = container_path_to_disk(path, run_data.run_dir)
        if is_vacuous(disk):
            continue
        assert disk is not None  # is_vacuous(None) is True, so disk is set here
        phase = phase_map.get(author, BefPhase.ORCHESTRATION).value
        dedup = (phase, path)
        if dedup in seen:
            continue
        seen.add(dedup)
        by_phase[phase].append(
            ArtifactRef(
                path=path,
                disk_path=str(disk),
                size_bytes=disk.stat().st_size,
                edited_by=author,
            )
        )
    return ArtifactsBySubtree(by=dict(by_phase))


# ---------------------------------------------------------------------------
# #6 — success criteria per subtree (three independent results)
# ---------------------------------------------------------------------------


def _has_nonvacuous_file(path: Path) -> bool:
    """A file is non-vacuous, or a directory contains any non-vacuous file."""
    if path.is_dir():
        return any(not is_vacuous(child) for child in path.rglob("*") if child.is_file())
    return not is_vacuous(path)


def key_file_exists(spec: str, run_dir: Path) -> bool:
    """Whether a canonical deliverable exists on disk.

    Existence means present **and non-vacuous**, except for deliverables that are
    legitimately empty on success (:data:`_MAY_BE_EMPTY`), which only need to be
    present. A trailing ``*`` in the filename is a prefix glob; a matching
    directory counts if it contains any non-vacuous file.
    """
    pure = PurePosixPath(spec)
    if "*" in pure.name:
        parent = container_path_to_disk(str(pure.parent), run_dir)
        if parent is None or not parent.is_dir():
            return False
        return any(_has_nonvacuous_file(match) for match in parent.glob(pure.name))
    disk = container_path_to_disk(spec, run_dir)
    if disk is None:
        return False
    if spec in _MAY_BE_EMPTY:
        return disk.is_file()
    return not is_vacuous(disk)


def success_criteria_by_bef(run_data: RunData) -> dict[str, dict[str, Any]]:
    """Per-subtree success: key-file existence, declared criteria, self-report.

    The three values are reported independently — they are NOT combined into a
    single pass/fail. ``key_files_exist`` checks the canonical deliverables on
    disk; ``declared_criteria`` is the subtree agents' ``success_criteria``;
    ``self_report`` is what those agents reported (completed/failed/verification).
    """
    phase_map = build_bef_phase_map(run_data.events, run_data.run_id)

    declared: dict[BefPhase, list[str]] = defaultdict(list)
    completed: dict[BefPhase, list[str]] = defaultdict(list)
    failed: dict[BefPhase, list[str]] = defaultdict(list)
    verif_passed: dict[BefPhase, int] = defaultdict(int)
    verif_failed: dict[BefPhase, list[dict[str, Any]]] = defaultdict(list)

    for event in run_data.events:
        phase = phase_map.get(event.aggregate_id)
        if phase is None:
            continue
        if isinstance(event, AgentCreated):
            criteria = event.success_criteria.strip()
            if criteria and criteria not in declared[phase]:
                declared[phase].append(criteria)
        elif isinstance(event, WorkCompleted):
            completed[phase].append(event.result)
        elif isinstance(event, WorkFailed):
            failed[phase].append(event.reason)
        elif isinstance(event, VerificationPassed):
            verif_passed[phase] += 1
        elif isinstance(event, VerificationFailed):
            verif_failed[phase].append(
                {"stage": event.failed_stage, "feedback": event.feedback, "score": event.score}
            )

    result: dict[str, dict[str, Any]] = {}
    dependency_contract = _dependency_contract_by_bef(run_data.events)
    for phase in _BEF_SUBTREES:
        result[phase.value] = {
            "key_files_exist": {
                spec: key_file_exists(spec, run_data.run_dir) for spec in KEY_FILES[phase]
            },
            "declared_criteria": list(declared.get(phase, [])),
            "dependency_contract": dependency_contract[phase],
            "self_report": {
                "work_completed": list(completed.get(phase, [])),
                "work_failed": list(failed.get(phase, [])),
                "verification_passed": verif_passed.get(phase, 0),
                "verification_failed": list(verif_failed.get(phase, [])),
            },
        }
    return result
