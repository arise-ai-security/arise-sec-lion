"""Idempotent one-shot migration from the old `output/` + `dataset-final/` +
`datasets-legacy/` layout to the new `runs/` + `experiments/<study>/` layout
described in docs/superpowers/specs/2026-04-22-experiments-layout-design.md §9.

Running twice produces the same final state. The migration is organized as a
two-phase operation: Phase A builds an in-memory plan with every collision
check up front; Phase B executes the plan. Under ``--dry-run`` Phase B is a
no-op — the plan drives the report without touching disk.

Tar archives produced by step 3 are best-effort historical snapshots. Tar
metadata is not normalized, so re-archiving the same source will not produce
a byte-identical output. That's fine for v1 — these are sealed reference
snapshots, not diffable artifacts.

Usage::

    python scripts/migrate_to_runs_layout.py --dry-run
    python scripts/migrate_to_runs_layout.py           # actually apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tarfile
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent

# Ensure `experiments.*` imports resolve when the script is invoked directly
# as `python scripts/migrate_to_runs_layout.py` (not just `python -m scripts.…`).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OUTPUT_DIR = REPO_ROOT / "output"
RUNS_DIR = REPO_ROOT / "runs"
LEGACY_POOL = RUNS_DIR / "_legacy"
DATASET_FINAL = REPO_ROOT / "dataset-final"
DATASETS_LEGACY = REPO_ROOT / "datasets-legacy"
LOGS_RUN_EVAL = REPO_ROOT / "logs" / "run_evaluation"
BRIEFING_OLD = REPO_ROOT / "experiments" / "configs" / "domain_briefing.md"
BRIEFING_NEW = REPO_ROOT / "prompts" / "domains" / "secbench" / "briefing.md"
DEPLOYMENT_HTML_OLD = REPO_ROOT / "deployment" / "output" / "data-flow-viz.html"
DEPLOYMENT_HTML_NEW = REPO_ROOT / "agent-docs" / "data-flow-viz.html"

STALE_EXPERIMENTS_SUBTREES = (
    REPO_ROOT / "experiments" / "__pycache__",
    REPO_ROOT / "experiments" / "baselines",
    REPO_ROOT / "experiments" / "tests",
    REPO_ROOT / "experiments" / "meeting-notes",
)

_REPORT_SCRIPT_NAMES = ("collect.py", "plot_success.py", "render_report.py")

# A-cell variants → baseline name used in the synthesized manifest.
_A_CELL_VARIANTS: dict[str, str] = {
    "A1": "claude-code-subagent",
    "A2": "claude-code-nosubagent",
}


logger = logging.getLogger(__name__)


# =============================================================================
# Plan primitives
# =============================================================================


@dataclass(frozen=True)
class CopyLegacyRun:
    """Copy a `dataset-final/…/<agent-uuid>/` subtree into `runs/_legacy/<uuid>/`.

    The migration writes a synthesized `run_manifest.json` alongside and then
    removes the source subtree so `dataset-final/` ends up empty.
    """

    source: Path
    target: Path
    manifest: dict[str, Any]
    run_id: str


@dataclass(frozen=True)
class RenameDir:
    source: Path
    target: Path


@dataclass(frozen=True)
class MoveEntry:
    source: Path
    target: Path


@dataclass(frozen=True)
class ArchiveTar:
    source: Path
    target: Path


@dataclass(frozen=True)
class WriteLegacyReadme:
    target: Path


@dataclass(frozen=True)
class MoveFile:
    source: Path
    target: Path


@dataclass(frozen=True)
class UnlinkDuplicateFile:
    """The destination exists and is byte-identical to the source — drop the source."""

    source: Path
    target: Path


@dataclass(frozen=True)
class DeleteTree:
    target: Path


@dataclass(frozen=True)
class EnrollLegacyRun:
    run_id: str
    study_id: str
    cell: str
    task: str
    attempt: int


@dataclass(frozen=True)
class RefreshSeedReports:
    study_id: str


PlanAction = (
    CopyLegacyRun
    | RenameDir
    | MoveEntry
    | ArchiveTar
    | WriteLegacyReadme
    | MoveFile
    | UnlinkDuplicateFile
    | DeleteTree
    | EnrollLegacyRun
    | RefreshSeedReports
)


@dataclass
class MigrationReport:
    """Summary of what the migration did (or would do, under --dry-run)."""

    actions: list[str] = field(default_factory=list)
    legacy_run_ids: dict[str, str] = field(default_factory=dict)  # rel path → run_id
    study_id: str = ""

    def record(self, message: str) -> None:
        logger.info(message)
        self.actions.append(message)


def _today_study_id() -> str:
    """Use the local date for the migrated study's id."""
    return f"{date.today().isoformat()}-initial-secbench"


def _current_git_sha() -> str | None:
    git_bin = shutil.which("git")
    if git_bin is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [git_bin, "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
            cwd=REPO_ROOT,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return result.stdout.strip() or None


# =============================================================================
# Phase A — plan builders (no filesystem mutation)
# =============================================================================


def _plan_output_to_runs() -> list[PlanAction]:
    """Either rename ``output/`` to ``runs/`` wholesale, or merge entries.

    Collisions (an entry in ``output/`` whose target already lives in ``runs/``)
    abort the whole migration — the caller surfaces them together.
    """
    if not OUTPUT_DIR.exists():
        return []
    if not RUNS_DIR.exists():
        return [RenameDir(source=OUTPUT_DIR, target=RUNS_DIR)]

    actions: list[PlanAction] = []
    collisions: list[tuple[Path, Path]] = []
    for entry in sorted(OUTPUT_DIR.iterdir()):
        target = RUNS_DIR / entry.name
        if target.exists():
            collisions.append((entry, target))
            continue
        actions.append(MoveEntry(source=entry, target=target))
    if collisions:
        lines = "\n".join(f"  {src} → {dst}" for src, dst in collisions)
        raise FileExistsError(
            "collision during output/→runs/ merge — targets already exist:\n" + lines
        )
    return actions


def _stable_uuid_from_path(*parts: str) -> uuid.UUID:
    """Derive a reproducible UUID from path components.

    A-cells don't carry a pre-existing agent UUID, but re-running the
    migration must produce the same UUID so the study manifest stays stable.
    We hash the parts with sha256 and use the first 16 bytes as a UUID4.
    """
    digest = hashlib.sha256("/".join(parts).encode("utf-8")).digest()[:16]
    # Mark as version-4-ish (random) and RFC4122 variant.
    mutable = bytearray(digest)
    mutable[6] = (mutable[6] & 0x0F) | 0x40
    mutable[8] = (mutable[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(mutable))


def _synthesize_manifest(
    *,
    run_id: str,
    kind: str,
    cell: str,
    task: str,
    attempt: int,
    study_id: str,
    variant: str | None = None,
    git_sha: str | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "kind": kind,
        "exit_status": "unknown",  # legacy data lacks reliable status info
        "cell": cell,
        "task": task,
        "attempt": attempt,
        "study_id": study_id,
        "git_sha": git_sha,
        "legacy_migration": True,
    }
    if variant is not None:
        manifest["variant"] = variant
    return manifest


def _plan_dataset_final(study_id: str, git_sha: str | None) -> list[CopyLegacyRun]:
    """Build the full source→target plan for ``dataset-final/`` migration.

    Collisions (non-idempotent) are collected up front and surfaced together
    so a single error lists every offending pair instead of aborting halfway.
    """
    if not DATASET_FINAL.exists():
        return []
    runs_dir = DATASET_FINAL / "runs"
    if not runs_dir.is_dir():
        return []

    planned: list[CopyLegacyRun] = []
    collisions: list[tuple[Path, Path]] = []

    for task_dir in sorted(runs_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        task = task_dir.name
        for cell_dir in sorted(task_dir.iterdir()):
            if not cell_dir.is_dir():
                continue
            cell = cell_dir.name
            for attempt_dir in sorted(cell_dir.iterdir()):
                if not attempt_dir.is_dir():
                    continue
                try:
                    attempt = int(attempt_dir.name)
                except ValueError:
                    logger.warning("skipping non-numeric attempt dir: %s", attempt_dir)
                    continue
                for entry in _plan_one_attempt(
                    attempt_dir=attempt_dir,
                    task=task,
                    cell=cell,
                    attempt=attempt,
                    study_id=study_id,
                    git_sha=git_sha,
                ):
                    if entry.target.exists() and not _already_migrated(entry.target):
                        collisions.append((entry.source, entry.target))
                        continue
                    planned.append(entry)

    if collisions:
        lines = "\n".join(f"  {src} → {dst}" for src, dst in collisions)
        raise FileExistsError(
            "collision during dataset-final migration — targets already exist "
            "and lack the legacy-migration marker:\n" + lines
        )
    return planned


def _plan_one_attempt(
    *,
    attempt_dir: Path,
    task: str,
    cell: str,
    attempt: int,
    study_id: str,
    git_sha: str | None,
) -> list[CopyLegacyRun]:
    is_a_cell = cell in _A_CELL_VARIANTS
    if is_a_cell:
        # A-cells don't have an agent-uuid layer — mint a stable UUID.
        run_uuid = _stable_uuid_from_path("a", task, cell, str(attempt))
        manifest = _synthesize_manifest(
            run_id=str(run_uuid),
            kind="claude_code_baseline",
            cell=cell,
            task=task,
            attempt=attempt,
            study_id=study_id,
            variant=_A_CELL_VARIANTS[cell],
            git_sha=git_sha,
        )
        return [
            CopyLegacyRun(
                source=attempt_dir,
                target=LEGACY_POOL / str(run_uuid),
                manifest=manifest,
                run_id=str(run_uuid),
            )
        ]

    # B-cells: one agent-uuid subdir per attempt.
    planned: list[CopyLegacyRun] = []
    for agent_dir in sorted(p for p in attempt_dir.iterdir() if p.is_dir()):
        try:
            run_uuid = uuid.UUID(agent_dir.name)
        except ValueError:
            logger.warning("skipping non-UUID agent dir: %s", agent_dir)
            continue
        manifest = _synthesize_manifest(
            run_id=str(run_uuid),
            kind="ours",
            cell=cell,
            task=task,
            attempt=attempt,
            study_id=study_id,
            git_sha=git_sha,
        )
        planned.append(
            CopyLegacyRun(
                source=agent_dir,
                target=LEGACY_POOL / str(run_uuid),
                manifest=manifest,
                run_id=str(run_uuid),
            )
        )
    return planned


def _already_migrated(target: Path) -> bool:
    manifest_path = target / "run_manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("legacy_migration"))


def _plan_archive_datasets_legacy() -> list[ArchiveTar]:
    if not DATASETS_LEGACY.exists():
        return []
    planned: list[ArchiveTar] = []
    for entry in sorted(DATASETS_LEGACY.iterdir()):
        if not entry.is_dir():
            continue
        archive = LEGACY_POOL / f"{entry.name}.tar.gz"
        if archive.exists():
            continue
        planned.append(ArchiveTar(source=entry, target=archive))
    return planned


def _plan_write_legacy_readme(
    copies: list[CopyLegacyRun], tars: list[ArchiveTar]
) -> list[PlanAction]:
    """Emit the README action if the legacy pool will be populated.

    Under ``--dry-run`` the pool may not exist yet, so we can't gate on disk
    state alone; we also gate on whether any other plan action will populate it.
    """
    readme = LEGACY_POOL / "README.md"
    if readme.exists():
        return []
    if not copies and not tars and not LEGACY_POOL.exists():
        return []
    return [WriteLegacyReadme(target=readme)]


def _bytes_equal(a: Path, b: Path) -> bool:
    return a.read_bytes() == b.read_bytes()


def _plan_relocate_file(source: Path, target: Path) -> list[PlanAction]:
    """Move ``source`` to ``target``. If both exist, compare bytes.

    Identical bytes = safe idempotent re-run, emit an unlink. Different bytes
    = refuse to silently discard data; raise.
    """
    if not source.exists():
        return []
    if not target.exists():
        return [MoveFile(source=source, target=target)]
    if source.is_file() and target.is_file() and _bytes_equal(source, target):
        return [UnlinkDuplicateFile(source=source, target=target)]
    raise FileExistsError(
        f"{target} exists with different content from {source}; refusing to "
        "silently discard. Inspect and resolve manually."
    )


def _tree_has_source_code(path: Path) -> bool:
    """True if the directory contains any ``*.py`` file outside ``__pycache__``."""
    for candidate in path.rglob("*.py"):
        if "__pycache__" in candidate.parts:
            continue
        return True
    return False


def _plan_cleanup_stale_trees() -> list[PlanAction]:
    """Plan deletions for stale trees named in the spec.

    Spec §9 declares these subtrees stale. Per the task: delete unconditionally
    unless a ``.py`` source file (outside ``__pycache__``) is present, which
    would indicate real code we shouldn't drop.
    """
    planned: list[PlanAction] = []
    for path in (LOGS_RUN_EVAL, *STALE_EXPERIMENTS_SUBTREES):
        if not path.exists():
            continue
        if path.is_file():
            planned.append(DeleteTree(target=path))
            continue
        if _tree_has_source_code(path):
            logger.warning(
                "skipping cleanup of %s — contains .py source files outside __pycache__",
                path,
            )
            continue
        planned.append(DeleteTree(target=path))
    return planned


def _plan_enroll_legacy_runs(
    study_id: str, copies: list[CopyLegacyRun]
) -> list[EnrollLegacyRun]:
    """Plan one enrollment action per migrated run.

    Sources two paths so re-running a partially-applied migration still
    enrolls everything:

    1. ``copies`` — runs about to be migrated in this invocation.
    2. Pre-existing ``runs/_legacy/<uuid>/run_manifest.json`` entries with the
       ``legacy_migration`` marker — runs migrated by an earlier invocation.

    Deduped by ``run_id``. register_run is idempotent, so even if the same
    run_id appears in both lists it's safe to emit both actions.

    Gated on the seed study's ``manifest.yaml`` existing — if the seed study
    hasn't been created yet (e.g. during a dev-environment migration), we
    skip enrollment entirely instead of emitting actions that would fail.
    """
    study_dir = REPO_ROOT / "experiments" / study_id
    if not (study_dir / "manifest.yaml").is_file():
        logger.info(
            "seed study at %s not present; skipping legacy-run enrollment",
            study_dir,
        )
        return []

    seen: set[str] = set()
    planned: list[EnrollLegacyRun] = []

    for copy in copies:
        manifest = copy.manifest
        cell = manifest.get("cell")
        task = manifest.get("task")
        attempt = manifest.get("attempt", 0)
        if not isinstance(cell, str) or not isinstance(task, str):
            continue
        if copy.run_id in seen:
            continue
        seen.add(copy.run_id)
        planned.append(
            EnrollLegacyRun(
                run_id=copy.run_id,
                study_id=study_id,
                cell=cell,
                task=task,
                attempt=int(attempt),
            )
        )

    if LEGACY_POOL.is_dir():
        for run_dir in sorted(LEGACY_POOL.iterdir()):
            if not run_dir.is_dir():
                continue
            run_id = run_dir.name
            if run_id in seen:
                continue
            manifest_path = run_dir / "run_manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not data.get("legacy_migration"):
                continue
            cell = data.get("cell")
            task = data.get("task")
            attempt = data.get("attempt", 0)
            if not isinstance(cell, str) or not isinstance(task, str):
                continue
            seen.add(run_id)
            planned.append(
                EnrollLegacyRun(
                    run_id=run_id,
                    study_id=study_id,
                    cell=cell,
                    task=task,
                    attempt=int(attempt),
                )
            )

    return planned


def _plan_refresh_seed_reports(study_id: str) -> list[RefreshSeedReports]:
    study_dir = REPO_ROOT / "experiments" / study_id
    if not (study_dir / "manifest.yaml").is_file():
        return []
    return [RefreshSeedReports(study_id=study_id)]


def build_plan(study_id: str, git_sha: str | None) -> list[PlanAction]:
    """Phase A: compute the full migration plan. Never mutates disk."""
    plan: list[PlanAction] = []
    plan.extend(_plan_output_to_runs())
    copies = _plan_dataset_final(study_id=study_id, git_sha=git_sha)
    plan.extend(copies)
    tars = _plan_archive_datasets_legacy()
    plan.extend(tars)
    plan.extend(_plan_write_legacy_readme(copies, tars))
    plan.extend(_plan_relocate_file(BRIEFING_OLD, BRIEFING_NEW))
    plan.extend(_plan_relocate_file(DEPLOYMENT_HTML_OLD, DEPLOYMENT_HTML_NEW))
    plan.extend(_plan_cleanup_stale_trees())
    plan.extend(_plan_enroll_legacy_runs(study_id, copies))
    plan.extend(_plan_refresh_seed_reports(study_id))
    return plan


# =============================================================================
# Phase B — plan executor
# =============================================================================


def _describe(action: PlanAction) -> str:  # noqa: PLR0911 — tagged dispatch
    if isinstance(action, CopyLegacyRun):
        return f"migrating {action.source} → {action.target}"
    if isinstance(action, RenameDir):
        return f"renaming {action.source} → {action.target}"
    if isinstance(action, MoveEntry):
        return f"moving {action.source} → {action.target}"
    if isinstance(action, ArchiveTar):
        return f"archiving {action.source} → {action.target}"
    if isinstance(action, WriteLegacyReadme):
        return f"writing {action.target}"
    if isinstance(action, MoveFile):
        return f"moving {action.source} → {action.target}"
    if isinstance(action, UnlinkDuplicateFile):
        return (
            f"destination up-to-date; removing duplicate source {action.source} "
            f"(matches {action.target})"
        )
    if isinstance(action, DeleteTree):
        return f"deleting {action.target}"
    if isinstance(action, EnrollLegacyRun):
        return (
            f"enrolling legacy run {action.run_id} into study {action.study_id} "
            f"(cell={action.cell}, task={action.task}, attempt={action.attempt})"
        )
    if isinstance(action, RefreshSeedReports):
        return (
            f"refreshing seed study reports for {action.study_id} "
            "(collect.py → plot_success.py → render_report.py)"
        )
    raise AssertionError(f"unhandled action type: {type(action).__name__}")


def _legacy_readme_body() -> str:
    git_sha = _current_git_sha() or "<unknown>"
    return (
        "# Legacy runs\n\n"
        "This directory holds run artifacts migrated from pre-2026-04 layouts:\n\n"
        "* `<uuid>/` subdirs — individual runs migrated from `dataset-final/` "
        "(B-cells keep their original boss agent UUID; A-cells get stable UUIDs "
        "derived from the path).\n"
        "* `*.tar.gz` archives — frozen snapshots of `datasets-legacy/{dataset,"
        "dataset-v2-20260420,dataset-v3-rerun,dataset-v3-rerun.archive-"
        "20260420-212259}/` that predate the current run manifest schema.\n\n"
        "These files are historical only. Do not promote a `_legacy/<uuid>/` "
        "run into the flat `runs/<uuid>/` pool without first confirming there "
        "is no live UUID collision.\n\n"
        f"Migrated at git sha `{git_sha}` via `scripts/migrate_to_runs_layout.py`.\n"
    )


def _copytree_with_symlink_warning(source: Path, target: Path) -> None:
    """Copy ``source`` to ``target`` preserving symlinks, warning when found.

    ``shutil.copytree`` with the default ``symlinks=False`` would silently
    follow symlinks and pull content from outside the source subtree. We
    preserve them verbatim and log a warning so the operator can review.
    """
    found_symlink = False
    for candidate in source.rglob("*"):
        if candidate.is_symlink():
            found_symlink = True
            break
    if found_symlink:
        logger.warning(
            "symlinks detected inside %s; preserving them as-is in %s",
            source,
            target,
        )
    shutil.copytree(source, target, symlinks=True)


def _execute_copy_legacy_run(action: CopyLegacyRun) -> None:
    if action.target.exists():
        # The collision check was in Phase A; reaching here means the target is
        # a completed prior-migration. Nothing to copy; still remove the source
        # so dataset-final/ ends up empty.
        if action.source.exists():
            shutil.rmtree(action.source)
        return
    LEGACY_POOL.mkdir(parents=True, exist_ok=True)
    _copytree_with_symlink_warning(action.source, action.target)
    manifest_path = action.target / "run_manifest.json"
    manifest_path.write_text(
        json.dumps(action.manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Spec §9: "move the <agent-uuid>/ subtree". Remove the source after a
    # successful copy so dataset-final/ ends up empty.
    if action.source.exists():
        shutil.rmtree(action.source)


def _execute_archive_tar(action: ArchiveTar) -> None:
    LEGACY_POOL.mkdir(parents=True, exist_ok=True)
    skipped: list[str] = []

    def _walk_and_add(tar: tarfile.TarFile, source: Path, arcname: str) -> None:
        try:
            tar.add(source, arcname=arcname, recursive=False)
        except (PermissionError, OSError) as exc:
            skipped.append(f"{source}: {exc}")
            return
        if source.is_dir() and not source.is_symlink():
            try:
                children = sorted(source.iterdir())
            except PermissionError as exc:
                skipped.append(f"{source}: {exc}")
                return
            for child in children:
                _walk_and_add(tar, child, f"{arcname}/{child.name}")

    with tarfile.open(action.target, "w:gz") as tar:
        _walk_and_add(tar, action.source, action.source.name)

    if skipped:
        logger.warning(
            "skipped %d unreadable path(s) while archiving %s: %s",
            len(skipped),
            action.source,
            "; ".join(skipped[:5]) + ("…" if len(skipped) > 5 else ""),
        )


def _execute_rename_dir(action: RenameDir) -> None:
    action.source.rename(action.target)


def _execute_move_entry(action: MoveEntry) -> None:
    shutil.move(str(action.source), str(action.target))


def _execute_write_readme(action: WriteLegacyReadme) -> None:
    action.target.parent.mkdir(parents=True, exist_ok=True)
    action.target.write_text(_legacy_readme_body(), encoding="utf-8")


def _execute_move_file(action: MoveFile) -> None:
    action.target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(action.source), str(action.target))
    parent = action.source.parent
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()


def _execute_unlink_duplicate(action: UnlinkDuplicateFile) -> None:
    action.source.unlink()
    parent = action.source.parent
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()


def _execute_delete_tree(action: DeleteTree) -> None:
    if action.target.is_file():
        action.target.unlink()
        return
    shutil.rmtree(action.target)


def _execute_enroll(action: EnrollLegacyRun, report: MigrationReport) -> None:
    from experiments.shared.scripts.register_run import register_run

    try:
        register_run(
            study_id=action.study_id,
            run_id=uuid.UUID(action.run_id),
            cell=action.cell,
            task=action.task,
            attempt=action.attempt,
            output_directory=LEGACY_POOL,
        )
    except Exception as exc:  # we log, surface, and continue
        logger.exception(
            "failed to enroll %s (cell=%s task=%s)",
            action.run_id,
            action.cell,
            action.task,
        )
        report.record(
            f"WARN: failed to enroll legacy run {action.run_id} "
            f"(cell={action.cell}, task={action.task}): {exc}"
        )


def _execute_refresh_reports(
    action: RefreshSeedReports, report: MigrationReport
) -> None:
    """Re-run the seed study's rendering pipeline after enrollment.

    The pre-enrollment manifest has stale sha256 pointers; regenerating
    collect → plot → render restores validator-green state. Individual
    script failures are non-fatal — migration has already applied all other
    changes successfully.
    """
    study_scripts_dir = REPO_ROOT / "experiments" / action.study_id / "scripts"
    for script_name in _REPORT_SCRIPT_NAMES:
        script_path = study_scripts_dir / script_name
        if not script_path.is_file():
            report.record(f"WARN: report refresh — {script_path} missing; skipping")
            continue
        try:
            subprocess.run(  # noqa: S603
                [sys.executable, str(script_path)],
                check=True,
                cwd=REPO_ROOT,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            logger.exception("report refresh failed for %s", script_name)
            report.record(
                f"WARN: report refresh — {script_name} failed: {exc}"
            )


def _execute(action: PlanAction, report: MigrationReport) -> None:  # noqa: PLR0911
    # Tagged dispatch: every PlanAction variant maps to exactly one handler.
    if isinstance(action, CopyLegacyRun):
        _execute_copy_legacy_run(action)
        rel = action.target.relative_to(REPO_ROOT).as_posix()
        report.legacy_run_ids[rel] = action.run_id
        return
    if isinstance(action, RenameDir):
        _execute_rename_dir(action)
        return
    if isinstance(action, MoveEntry):
        _execute_move_entry(action)
        return
    if isinstance(action, ArchiveTar):
        _execute_archive_tar(action)
        return
    if isinstance(action, WriteLegacyReadme):
        _execute_write_readme(action)
        return
    if isinstance(action, MoveFile):
        _execute_move_file(action)
        return
    if isinstance(action, UnlinkDuplicateFile):
        _execute_unlink_duplicate(action)
        return
    if isinstance(action, DeleteTree):
        _execute_delete_tree(action)
        return
    if isinstance(action, EnrollLegacyRun):
        _execute_enroll(action, report)
        return
    if isinstance(action, RefreshSeedReports):
        _execute_refresh_reports(action, report)
        return
    raise AssertionError(f"unhandled action type: {type(action).__name__}")


def _execute_plan(
    plan: list[PlanAction], report: MigrationReport, *, dry_run: bool
) -> None:
    """Phase B: run the plan, or simulate it under ``--dry-run``."""
    for action in plan:
        report.record(_describe(action))
        if dry_run:
            if isinstance(action, CopyLegacyRun):
                rel = action.target.relative_to(REPO_ROOT).as_posix()
                report.legacy_run_ids[rel] = action.run_id
            continue
        _execute(action, report)

    # Output/ is cleared by the RenameDir/MoveEntry actions. Remove the now-
    # empty directory as a post-step. Dry-run leaves disk untouched.
    if not dry_run and OUTPUT_DIR.exists():
        try:
            entries = list(OUTPUT_DIR.iterdir())
        except OSError:
            return
        if not entries:
            OUTPUT_DIR.rmdir()


# =============================================================================
# Public entry points
# =============================================================================


def refresh_seed_study_reports(study_id: str, *, dry_run: bool) -> MigrationReport:
    """Re-run the seed study's rendering pipeline so report shas stay valid.

    Exposed as a standalone helper for callers that want to refresh reports
    without running the full migration.
    """
    report = MigrationReport(study_id=study_id)
    for action in _plan_refresh_seed_reports(study_id):
        report.record(_describe(action))
        if not dry_run:
            _execute_refresh_reports(action, report)
    return report


def run(*, dry_run: bool) -> MigrationReport:
    report = MigrationReport(study_id=_today_study_id())
    git_sha = _current_git_sha()
    plan = build_plan(study_id=report.study_id, git_sha=git_sha)
    _execute_plan(plan, report, dry_run=dry_run)
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_to_runs_layout",
        description="One-shot idempotent migration to the runs/ + experiments/ layout.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the migration plan without modifying the filesystem",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="migrate: %(message)s")
    args = _build_arg_parser().parse_args(argv)
    try:
        report = run(dry_run=args.dry_run)
    except FileExistsError as exc:
        print(f"migrate: collision detected — {exc}", file=sys.stderr)
        return 1
    mode = "planned" if args.dry_run else "applied"
    print(f"migrate: {mode} {len(report.actions)} action(s); study={report.study_id}")
    if not args.dry_run and DATASET_FINAL.exists():
        print(
            f"migrate: NOTE — {DATASET_FINAL.relative_to(REPO_ROOT)}/ has been "
            "moved, not copied. Remove any remaining empty scaffolding with "
            "`find dataset-final -type d -empty -delete` if desired."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
