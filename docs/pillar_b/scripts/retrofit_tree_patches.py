"""Retrofit B-cell tree runs so the mechanical evaluator can grade them.

The tree orchestrator stores patches at ``<run_dir>/<agent_id>/testcase/fix.patch``
(or similar names) whereas ``experiments.mechanical_evaluator.evaluate_run``
bind-mounts ``<run_dir>/workspace/`` into the docker eval image and expects
``model_patch.diff`` there. This script:

1. Loads ``experiments/locked_instances.yaml`` to resolve each CVE's docker image.
2. Iterates over every B-cell run in ``dataset/INDEX.jsonl``.
3. Finds the best-available patch under ``<run_dir>/*/testcase/`` and copies it
   to ``<run_dir>/workspace/model_patch.diff`` (also copies ``repro.sh`` when
   present so the repro phase can run).
4. Re-invokes ``evaluate_run`` and rewrites ``mechanical.json``.
5. Rewrites the matching INDEX.jsonl line's ``mechanical_pass`` fields.
6. Emits a summary CSV of the retrofit outcome.

A-cell runs are NOT touched. Runs with no on-disk patch keep
``mechanical_pass=False`` but are tagged ``no_patch_produced=True`` in the
summary CSV so downstream analysis can distinguish "agent never wrote a patch"
from "agent wrote a bad patch".
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from experiments.mechanical_evaluator import (  # noqa: E402
    DOCKER_ERROR_MARKERS,
    classify_sanitizer_output,
)
from experiments.run_experiment import _normalize_sanitizer_error  # noqa: E402
from plugins.security import resolve_secbench_image  # noqa: E402


import subprocess  # noqa: E402


def hydrate_workspace_from_image(workspace: Path, docker_image: str) -> None:
    """Copy the image's baked-in ``/testcase/`` contents into ``workspace`` so
    that our bind-mount at ``/testcase`` does not shadow the image's pre-placed
    ``poc``, ``base_commit_hash``, etc. Only fills in files that are missing
    from ``workspace`` — never overwrites.

    Uses ``docker create`` + ``docker cp`` (container can be unused; `docker cp`
    works without starting it) to dump the layer contents.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    # A quick existence check to short-circuit when already hydrated.
    if (workspace / "base_commit_hash").exists() and (
        (workspace / "poc").exists()
        or any(workspace.glob("poc*"))
        or any(workspace.glob("*.zip"))
    ):
        return

    try:
        created = subprocess.run(  # noqa: S603
            ["docker", "create", docker_image, "true"],
            capture_output=True,
            check=True,
            timeout=60,
        )
        cid = created.stdout.decode().strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("docker create failed for %s: %s", docker_image, exc)
        return

    try:
        subprocess.run(  # noqa: S603
            ["docker", "cp", f"{cid}:/testcase/.", str(workspace)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "docker cp failed: %s (stderr=%s)",
            exc,
            exc.stderr.decode(errors="replace")[:200] if exc.stderr else "",
        )
    finally:
        subprocess.run(  # noqa: S603
            ["docker", "rm", cid],
            capture_output=True,
            check=False,
            timeout=60,
        )


def _run_secb_fixed(
    phase: str,
    *,
    workspace: Path,
    docker_image: str,
    timeout_sec: float = 300.0,
) -> subprocess.CompletedProcess[bytes]:
    """Drop-in replacement for experiments.mechanical_evaluator._run_secb that
    bind-mounts ``workspace`` to ``/testcase`` (matching where the ``secb`` bash
    script reads ``model_patch.diff`` / ``repro.sh``) instead of ``/workspace``.

    The upstream evaluator's ``workspace:/workspace`` mount is a latent bug:
    ``/usr/local/bin/secb`` inside each ``secb-tools:*-patch`` image reads
    exclusively from ``/testcase/``. Fixing it here keeps
    ``experiments/mechanical_evaluator.py`` untouched (scope: Pillar B does not
    modify run-execution code).
    """
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "-v",
        f"{workspace}:/testcase",
        "-w",
        "/src",
        docker_image,
        "secb",
        phase,
    ]
    try:
        return subprocess.run(  # noqa: S603 -- args validated upstream
            cmd,
            capture_output=True,
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("secb %s timed out after %.1fs", phase, timeout_sec)
        return subprocess.CompletedProcess(
            cmd,
            returncode=124,
            stdout=exc.stdout or b"",
            stderr=exc.stderr or b"",
        )


def _detect_docker_error(stderr: bytes) -> str | None:
    text = stderr.decode("utf-8", errors="replace")
    for marker in DOCKER_ERROR_MARKERS:
        if marker in text:
            return marker
    return None


def _run_bash_persistent(
    script: str,
    *,
    workspace: Path,
    docker_image: str,
    timeout_sec: float = 900.0,
) -> subprocess.CompletedProcess[bytes]:
    """Run a bash script in a single docker container with workspace:/testcase mount.

    Running ``secb build``, ``secb repro``, ``secb patch`` as three separate
    ``docker run`` calls — as the upstream evaluator does — discards each
    container's built state, so ``secb repro`` can never find the binaries
    ``secb build`` just produced. Combining all three into one container run
    preserves the filesystem between phases and yields the correct pipeline
    semantics.
    """
    cmd = [
        "docker", "run", "--rm", "--network=none",
        "-v", f"{workspace}:/testcase",
        "-w", "/src",
        docker_image,
        "bash", "-c", script,
    ]
    try:
        return subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("bash run timed out after %.1fs", timeout_sec)
        return subprocess.CompletedProcess(
            cmd,
            returncode=124,
            stdout=exc.stdout or b"",
            stderr=exc.stderr or b"",
        )


PERSISTENT_SCRIPT = r"""
set -o pipefail
rc_build=0
rc_repro=0
rc_patch=0

echo "=== PHASE build ==="
secb build
rc_build=$?
echo "=== END PHASE build rc=$rc_build ==="

echo "=== PHASE repro ==="
# secb repro intentionally uses no explicit exit codes and is expected to
# trigger sanitizer output via stderr/stdout; capture the exit code but don't
# gate the next phase on it.
secb repro
rc_repro=$?
echo "=== END PHASE repro rc=$rc_repro ==="

echo "=== PHASE patch ==="
secb patch
rc_patch=$?
echo "=== END PHASE patch rc=$rc_patch ==="

echo "=== PHASE repro_after_patch ==="
# Re-run repro after patch to verify the fix actually suppresses the sanitizer
# error. Used by fixer_pass logic.
secb repro
rc_repro_after=$?
echo "=== END PHASE repro_after_patch rc=$rc_repro_after ==="

echo "___RETROFIT_RESULT___rc_build=$rc_build rc_repro=$rc_repro rc_patch=$rc_patch rc_repro_after=$rc_repro_after"
exit 0
"""


def _parse_persistent_output(
    proc: subprocess.CompletedProcess[bytes],
) -> dict:
    """Split a persistent-run combined-bash output into per-phase sections.

    Returns keys: build_out/err/rc, repro_out/err/rc, patch_out/err/rc,
    repro_after_out/err/rc. Splits the interleaved stdout/stderr by our
    ``=== PHASE name ===`` markers; stderr is returned as-is attached to
    whichever phase was running last at emit time (best-effort).
    """
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    # Parse rc line
    rc = {"rc_build": -1, "rc_repro": -1, "rc_patch": -1, "rc_repro_after": -1}
    for line in reversed(out.splitlines()):
        if line.startswith("___RETROFIT_RESULT___"):
            for part in line.removeprefix("___RETROFIT_RESULT___").strip().split():
                k, _, v = part.partition("=")
                rc[k] = int(v)
            break
    # Segment stdout by phase markers
    phases: dict[str, list[str]] = {
        "build": [],
        "repro": [],
        "patch": [],
        "repro_after_patch": [],
    }
    active = None
    for line in out.splitlines():
        if line.startswith("=== PHASE ") and line.endswith(" ==="):
            active = line.removeprefix("=== PHASE ").removesuffix(" ===").strip()
            continue
        if line.startswith("=== END PHASE ") and line.endswith(" ==="):
            active = None
            continue
        if active in phases:
            phases[active].append(line)
    return {
        "build_stdout": "\n".join(phases["build"]),
        "repro_stdout": "\n".join(phases["repro"]),
        "patch_stdout": "\n".join(phases["patch"]),
        "repro_after_stdout": "\n".join(phases["repro_after_patch"]),
        "stderr_all": err,
        **rc,
    }


def evaluate_run_fixed(
    *,
    workspace: Path,
    docker_image: str,
    expected_sanitizer_error: str,
) -> dict:
    """Drop-in replacement for :func:`evaluate_run` with two fixes:

    1. Bind-mount ``workspace`` to ``/testcase`` (not ``/workspace``) to match
       where the ``secb`` script reads ``model_patch.diff`` / ``repro.sh``.
    2. Run all three phases in a single persistent container so that the
       binaries built by ``secb build`` are still present when ``secb repro``
       runs.

    ``fixer_pass`` is redefined as:
    ``patch applied (rc_patch == 0)`` **AND** ``repro-after-patch no longer
    triggers the expected sanitizer error``. This matches the semantics
    documented in the design spec §5.1 ("secb patch exit 0 means the patch
    compiles AND the sanitizer error is gone").
    """
    expected_lower = expected_sanitizer_error.lower()
    workspace_abs = workspace.resolve(strict=True)
    hydrate_workspace_from_image(workspace_abs, docker_image)

    proc = _run_bash_persistent(
        PERSISTENT_SCRIPT,
        workspace=workspace_abs,
        docker_image=docker_image,
        timeout_sec=900.0,
    )
    parsed = _parse_persistent_output(proc)

    repro_out = (parsed["repro_stdout"] + "\n" + parsed["stderr_all"]).encode()
    repro_after_out = (parsed["repro_after_stdout"] + "\n" + parsed["stderr_all"]).encode()
    repro_class = classify_sanitizer_output(repro_out)
    repro_after_class = classify_sanitizer_output(repro_after_out)

    builder_pass = parsed["rc_build"] == 0
    exploiter_pass = repro_class is not None and repro_class.lower() == expected_lower
    patch_applied = parsed["rc_patch"] == 0
    fixer_pass = bool(
        patch_applied
        and (repro_after_class is None or repro_after_class.lower() != expected_lower)
        and exploiter_pass
    )
    end_to_end_pass = bool(builder_pass and exploiter_pass and fixer_pass)

    result = {
        "builder_pass": builder_pass,
        "exploiter_pass": exploiter_pass,
        "fixer_pass": fixer_pass,
        "end_to_end_pass": end_to_end_pass,
        "details": {
            "build_exit": parsed["rc_build"],
            "repro_exit": parsed["rc_repro"],
            "repro_detected_class": repro_class,
            "repro_expected_class": expected_lower,
            "patch_exit": parsed["rc_patch"],
            "repro_after_patch_exit": parsed["rc_repro_after"],
            "repro_after_detected_class": repro_after_class,
        },
    }
    build_docker_err = _detect_docker_error(proc.stderr)
    if build_docker_err is not None:
        result["details"]["docker_error"] = build_docker_err
    logger.info(
        "builder=%s exploiter=%s fixer=%s (patch_rc=%d repro_rc=%d repro_after_rc=%d)",
        builder_pass, exploiter_pass, fixer_pass,
        parsed["rc_patch"], parsed["rc_repro"], parsed["rc_repro_after"],
    )
    return result


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("retrofit")


DATASET_ROOT = REPO_ROOT / "dataset"
RUNS_ROOT = DATASET_ROOT / "runs"
INDEX_PATH = DATASET_ROOT / "INDEX.jsonl"
LOCKED_PATH = DATASET_ROOT / "locked_instances.yaml"

PATCH_CANDIDATE_NAMES: tuple[str, ...] = (
    "fix.patch",
    "model_patch.diff",
)
PATCH_GLOB_PATTERNS: tuple[str, ...] = (
    "cve-*.patch",
    "CVE-*.patch",
    "*.patch",
    "repo_changes.diff",
)


@dataclass(frozen=True)
class PatchSource:
    """One candidate patch file discovered on disk, with its selection priority."""

    path: Path
    priority: int  # lower is better

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def discover_patch(run_dir: Path) -> PatchSource | None:
    """Pick the most authoritative patch file under ``<run_dir>/*/testcase/``.

    Priority order (lower wins): explicit ``fix.patch`` > named
    ``cve-*.patch`` > any ``*.patch`` > ``repo_changes.diff``.
    Ties break on larger file size (patches with more hunks tend to be the
    real deliverable; empty files lose).
    """
    candidates: list[PatchSource] = []
    for testcase in run_dir.glob("*/testcase"):
        if not testcase.is_dir():
            continue
        for name in PATCH_CANDIDATE_NAMES:
            f = testcase / name
            if f.is_file() and f.stat().st_size > 0:
                pri = 0 if name == "fix.patch" else 1
                candidates.append(PatchSource(path=f, priority=pri))
        for pat in PATCH_GLOB_PATTERNS:
            for f in testcase.glob(pat):
                if f.name in PATCH_CANDIDATE_NAMES:
                    continue
                if not f.is_file() or f.stat().st_size == 0:
                    continue
                if pat.startswith("cve") or pat.startswith("CVE"):
                    pri = 2
                elif pat == "*.patch":
                    pri = 3
                else:
                    pri = 4
                candidates.append(PatchSource(path=f, priority=pri))

    if not candidates:
        return None
    candidates.sort(key=lambda c: (c.priority, -c.size))
    return candidates[0]


def discover_repro(run_dir: Path) -> Path | None:
    """Return the first ``repro.sh`` found under ``<run_dir>/*/testcase/``."""
    for testcase in run_dir.glob("*/testcase"):
        if not testcase.is_dir():
            continue
        f = testcase / "repro.sh"
        if f.is_file() and f.stat().st_size > 0:
            return f
    return None


def load_locked_instances() -> dict[str, dict]:
    """Index ``locked_instances.yaml`` by ``instance_id`` (cve_id)."""
    raw = yaml.safe_load(LOCKED_PATH.read_text(encoding="utf-8"))
    return {item["instance_id"]: item for item in raw["instances"]}


def retrofit_one_run(
    run_dir: Path,
    cve_entry: dict,
    *,
    skip_eval: bool = False,
) -> dict:
    """Retrofit a single B-cell run; return a summary dict.

    Summary keys: run_id, patch_source, patch_size_bytes, repro_source,
    no_patch_produced, docker_image, mechanical_pass_before, mechanical_pass_after,
    error.
    """
    summary: dict = {
        "run_id": run_dir.parent.parent.name + "-" + run_dir.parent.name + "-" + run_dir.name,
        "run_dir": str(run_dir.relative_to(REPO_ROOT)),
        "patch_source": None,
        "patch_size_bytes": 0,
        "repro_source": None,
        "no_patch_produced": True,
        "docker_image": None,
        "mechanical_pass_before": None,
        "mechanical_pass_after": None,
        "error": None,
    }

    existing = run_dir / "mechanical.json"
    if existing.exists():
        try:
            summary["mechanical_pass_before"] = json.loads(existing.read_text())
        except json.JSONDecodeError:
            pass

    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    patch = discover_patch(run_dir)
    if patch is None:
        summary["no_patch_produced"] = True
        logger.info("NO PATCH — %s", summary["run_id"])
    else:
        dest = workspace / "model_patch.diff"
        shutil.copy2(patch.path, dest)
        summary["patch_source"] = str(patch.path.relative_to(run_dir))
        summary["patch_size_bytes"] = patch.size
        summary["no_patch_produced"] = False
        logger.info(
            "PATCH %s (%d B) -> %s",
            summary["patch_source"],
            patch.size,
            dest.relative_to(run_dir),
        )

    repro = discover_repro(run_dir)
    if repro is not None:
        dest_repro = workspace / "repro.sh"
        shutil.copy2(repro, dest_repro)
        summary["repro_source"] = str(repro.relative_to(run_dir))

    base_image = cve_entry["docker_image"]
    if not base_image.startswith("hwiwonlee") and not base_image.startswith("secb"):
        # locked_instances stores e.g. hwiwonlee/secb.eval.x86_64.njs.cve-2022-32414
        base_image = f"hwiwonlee/secb.eval.x86_64.{cve_entry['instance_id']}"
    if ":" not in base_image:
        base_image = f"{base_image}:patch"
    docker_image = resolve_secbench_image(base_image, security_tools_enabled=True)
    summary["docker_image"] = docker_image

    if skip_eval:
        return summary

    try:
        sanitizer = _normalize_sanitizer_error(str(cve_entry["expected_sanitizer_error"]))
        report = evaluate_run_fixed(
            workspace=workspace,
            docker_image=docker_image,
            expected_sanitizer_error=sanitizer,
        )
        existing.write_text(json.dumps(report, indent=2), encoding="utf-8")
        summary["mechanical_pass_after"] = {
            "builder": bool(report["builder_pass"]),
            "exploiter": bool(report["exploiter_pass"]),
            "fixer": bool(report["fixer_pass"]),
            "end_to_end": bool(report["end_to_end_pass"]),
        }
    except Exception as exc:  # noqa: BLE001 -- log and continue
        summary["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("Eval failed for %s: %s", summary["run_id"], summary["error"])

    return summary


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--only", help="Run only this run_id (for smoke testing)", default=None
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Stage patches but do NOT re-run evaluator or rewrite INDEX.",
    )
    args = ap.parse_args()

    if not INDEX_PATH.exists():
        logger.error("INDEX.jsonl missing at %s", INDEX_PATH)
        return 1

    locked = load_locked_instances()

    rows = [json.loads(line) for line in INDEX_PATH.read_text().splitlines() if line.strip()]
    b_rows = [r for r in rows if r["cell"] in {"B1", "B2"}]
    if args.only:
        b_rows = [r for r in b_rows if r["run_id"] == args.only]
    logger.info("Found %d B-cell runs to process", len(b_rows))

    summaries: list[dict] = []
    for idx, row in enumerate(b_rows, start=1):
        cve_id = row["cve_id"]
        run_dir = Path(row["path"])
        if not run_dir.is_absolute():
            run_dir = REPO_ROOT / run_dir
        if not run_dir.exists():
            logger.warning("run_dir missing: %s", run_dir)
            continue
        if cve_id not in locked:
            logger.warning("cve_id %s not in locked_instances; skipping", cve_id)
            continue
        logger.info("[%d/%d] Retrofitting %s", idx, len(b_rows), row["run_id"])
        s = retrofit_one_run(run_dir, locked[cve_id], skip_eval=args.dry_run)
        summaries.append(s)
        if s["mechanical_pass_after"] is not None:
            row["mechanical_pass"] = s["mechanical_pass_after"]

    if not args.dry_run and not args.only:
        INDEX_PATH.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )
        logger.info("Rewrote %s (%d rows)", INDEX_PATH, len(rows))
    elif args.only:
        # partial rewrite: update the single matching row in-place
        orig = [json.loads(line) for line in INDEX_PATH.read_text().splitlines() if line.strip()]
        updated = {r["run_id"]: r for r in rows}
        for i, r in enumerate(orig):
            if r["run_id"] in updated:
                orig[i] = updated[r["run_id"]]
        INDEX_PATH.write_text(
            "\n".join(json.dumps(r) for r in orig) + "\n",
            encoding="utf-8",
        )
        logger.info("Partial INDEX.jsonl update applied for %s", args.only)

    summary_csv = REPO_ROOT / "docs" / "pillar_b" / "tables" / "retrofit_summary.csv"
    summary_csv.parent.mkdir(parents=True, exist_ok=True)
    flat_rows = []
    for s in summaries:
        flat = dict(s)
        flat["pass_before"] = json.dumps(flat.pop("mechanical_pass_before") or {})
        flat["pass_after"] = json.dumps(flat.pop("mechanical_pass_after") or {})
        flat_rows.append(flat)
    if flat_rows:
        with summary_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(flat_rows[0].keys()))
            writer.writeheader()
            writer.writerows(flat_rows)
        logger.info("Wrote retrofit summary: %s", summary_csv)

    passed_after = sum(
        1 for s in summaries
        if s["mechanical_pass_after"] and s["mechanical_pass_after"].get("end_to_end")
    )
    has_patch = sum(1 for s in summaries if not s["no_patch_produced"])
    logger.info(
        "Done. runs=%d has_patch=%d end_to_end_pass=%d",
        len(summaries), has_patch, passed_after,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
