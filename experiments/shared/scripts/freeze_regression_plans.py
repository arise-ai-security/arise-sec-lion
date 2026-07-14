"""Generate and freeze configuration-independent container regression plans.

For every ``(instance_id, base_commit)`` in a configured study:

1. Resolve the SEC-bench ``:patch`` image and record its digest.
2. Select a project-specific executable behavior probe that is never derived
   from run artifacts.
3. Validate the command on the **base** state (must pass) and on the
   **gold-patched** state (must pass).
4. Freeze argv, timeouts, plan hash, generator model, dataset revision,
   and image digest into ``regression_plans.yaml``.

Usage::

    uv run python -m experiments.shared.scripts.freeze_regression_plans \\
        --study /path/to/study \\
        --dataset-revision SEC-bench/SEC-bench@eval
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from experiments.shared.evaluation.regression import (
    ContainerRegressionRunner,
    FrozenRegressionPlan,
    RegressionCommand,
    default_secbench_image,
    plan_sha256,
    regression_evidence_sha256,
)
from experiments.shared.scripts._paths import get_repo_root


logger = logging.getLogger(__name__)

_PROJECT_PROBES: dict[str, tuple[tuple[str, ...], float]] = {
    "faad2": (
        (
            "bash",
            "-lc",
            "./frontend/faad -h 2>&1 | grep -q 'Ahead Software MPEG-4 AAC Decoder'",
        ),
        60.0,
    ),
    "libredwg": (
        ("bash", "-lc", "./programs/dwgread --help 2>&1 | grep -qi dwgread"),
        60.0,
    ),
    "mruby": (
        (
            "./build/host/bin/mruby",
            "-e",
            "raise unless [1,2,3].map { |x| x * 2 } == [2,4,6]",
        ),
        60.0,
    ),
    "mupdf": (
        ("bash", "-lc", "/out/mupdf/mutool -v 2>&1 | grep -q 'mutool version'"),
        60.0,
    ),
    "njs": (
        (
            "bash",
            "-lc",
            "./build/njs -v 2>&1 | grep -Eq '^[0-9]+\\.[0-9]+\\.[0-9]+'",
        ),
        60.0,
    ),
    "openjpeg": (
        (
            "bash",
            "-lc",
            "./build/bin/opj_compress -h 2>&1 | grep -qi 'JPEG 2000'",
        ),
        60.0,
    ),
    "upx": (("bash", "-lc", "./build/debug/upx --version 2>&1 | grep -qi UPX"), 60.0),
    "wasm3": (("bash", "-lc", "./build/wasm3 --version 2>&1 | grep -qi wasm3"), 60.0),
    "yara": (("bash", "-lc", "./yara --version 2>&1 | grep -Eq '^[0-9]+\\.'"), 60.0),
}

_GENERATOR_MODEL = "container-probe-v1"
_DEFAULT_TIMEOUT = 120.0


def _load_gold_patches_from_hf_cache() -> dict[str, str]:
    """Load gold patches from a local HuggingFace hub cache of SEC-bench."""
    root = Path.home() / ".cache/huggingface/hub/datasets--SEC-bench--SEC-bench/blobs"
    patches: dict[str, str] = {}
    if not root.is_dir():
        return patches
    for blob in root.iterdir():
        if not blob.is_file() or blob.stat().st_size < 1000:
            continue
        try:
            text = blob.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.lstrip().startswith("{"):
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            instance_id = row.get("instance_id")
            patch = row.get("patch")
            if isinstance(instance_id, str) and isinstance(patch, str) and patch.strip():
                patches[instance_id] = patch
    return patches


def _load_cohort(study_dir: Path) -> list[dict[str, Any]]:
    dataset = yaml.safe_load((study_dir / "dataset.yaml").read_text(encoding="utf-8"))
    if not isinstance(dataset, dict):
        raise ValueError("dataset.yaml must be a mapping")
    gold = _load_gold_patches_from_hf_cache()
    instances: list[dict[str, Any]] = []
    for path_str in dataset.get("source", {}).get("paths", []):
        path = get_repo_root() / path_str
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("patch") and data.get("instance_id") in gold:
            data = {**data, "patch": gold[str(data["instance_id"])]}
        instances.append(data)
    if not instances:
        raise ValueError("no fixtures listed under dataset.source.paths")
    return instances


def _image_digest(image: str) -> str | None:
    completed = subprocess.run(  # noqa: S603
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}",
            image,
        ],
        capture_output=True,
        text=True,
        timeout=60.0,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return (completed.stdout or "").strip() or None


def _ensure_image(image: str, *, pull: bool) -> str | None:
    digest = _image_digest(image)
    if digest is not None:
        return digest
    if not pull:
        return None
    pull_result = subprocess.run(  # noqa: S603
        ["docker", "pull", image],
        capture_output=True,
        text=True,
        timeout=3600.0,
        check=False,
    )
    if pull_result.returncode != 0:
        logger.error(
            "pull failed: %s",
            (pull_result.stderr or pull_result.stdout or "").strip(),
        )
        return None
    return _image_digest(image)


def _write_gold_patch(fixture: dict[str, Any], dest: Path) -> Path | None:
    patch = fixture.get("patch")
    if not isinstance(patch, str) or not patch.strip():
        return None
    dest.write_text(patch if patch.endswith("\n") else patch + "\n", encoding="utf-8")
    return dest


def _discover_and_validate(
    *,
    instance_id: str,
    project_name: str,
    base_commit: str,
    image: str,
    image_digest: str,
    dataset_revision: str,
    work_dir: str,
    gold_patch: Path | None,
) -> tuple[FrozenRegressionPlan, dict[str, Any], dict[str, Any]] | None:
    """Validate one project-specific behavioral probe on base and gold."""
    candidate = _PROJECT_PROBES.get(project_name)
    if candidate is None or gold_patch is None:
        return None
    argv, timeout = candidate
    commands = (
        RegressionCommand(argv=argv, timeout_seconds=timeout, required=True, cwd="."),
    )
    temporary = FrozenRegressionPlan(
        instance_id=instance_id,
        base_commit=base_commit,
        commands=commands,
        plan_sha256=plan_sha256(
            instance_id,
            base_commit,
            commands,
            work_dir=work_dir,
            dataset_revision=dataset_revision,
            image_digest=image_digest,
        ),
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        work_dir=work_dir,
        generator_model=_GENERATOR_MODEL,
        dataset_revision=dataset_revision,
        image_digest=image_digest,
    )
    base_evidence = ContainerRegressionRunner(apply_patch=False, rebuild=True).run(
        temporary, patch_file=None, patch_hash=None, image=image
    )
    if not (base_evidence.available and base_evidence.passed):
        logger.error("base reject %s: %s", list(argv), base_evidence.reason)
        return None
    gold_hash = hashlib.sha256(gold_patch.read_bytes()).hexdigest()
    gold_evidence = ContainerRegressionRunner(apply_patch=True, rebuild=True).run(
        temporary,
        patch_file=gold_patch,
        patch_hash=gold_hash,
        image=image,
    )
    if not (gold_evidence.available and gold_evidence.passed):
        logger.error("gold reject %s: %s", list(argv), gold_evidence.reason)
        return None
    base_validation = regression_evidence_sha256(base_evidence)
    gold_validation = regression_evidence_sha256(gold_evidence)
    frozen = FrozenRegressionPlan(
        instance_id=instance_id,
        base_commit=base_commit,
        commands=commands,
        plan_sha256=plan_sha256(
            instance_id,
            base_commit,
            commands,
            work_dir=work_dir,
            dataset_revision=dataset_revision,
            image_digest=image_digest,
            base_validation_sha256=base_validation,
            gold_validation_sha256=gold_validation,
        ),
        generated_at=temporary.generated_at,
        work_dir=work_dir,
        generator_model=_GENERATOR_MODEL,
        dataset_revision=dataset_revision,
        image_digest=image_digest,
        base_validation_sha256=base_validation,
        gold_validation_sha256=gold_validation,
    )
    return frozen, base_evidence.to_dict(), gold_evidence.to_dict()


def freeze_plans(
    *,
    study_dir: Path,
    dataset_revision: str,
    pull: bool,
    output: Path,
) -> int:
    fixtures = _load_cohort(study_dir)
    frozen: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []
    failures: list[str] = []
    tmp = study_dir / ".regression-plan-tmp"
    tmp.mkdir(exist_ok=True)
    try:
        for fixture in fixtures:
            instance_id = str(fixture["instance_id"])
            project_name = str(fixture["project_name"])
            base_commit = str(fixture["base_commit"])
            work_dir = str(fixture.get("work_dir") or "/src")
            image = default_secbench_image(instance_id)
            logger.info("%s @ %s", instance_id, base_commit[:12])
            digest = _ensure_image(image, pull=pull)
            if digest is None:
                failures.append(f"{instance_id}: image unavailable ({image})")
                logger.error("image unavailable: %s", image)
                continue
            gold_path = _write_gold_patch(fixture, tmp / f"{instance_id}.gold.diff")
            validated = _discover_and_validate(
                instance_id=instance_id,
                project_name=project_name,
                base_commit=base_commit,
                image=image,
                image_digest=digest,
                dataset_revision=dataset_revision,
                work_dir=work_dir,
                gold_patch=gold_path,
            )
            if validated is None:
                failures.append(f"{instance_id}: no probe passed base+gold")
                logger.error("no valid probe: %s", instance_id)
                continue
            plan, base_validation, gold_validation = validated
            frozen.append(
                {
                    "instance_id": plan.instance_id,
                    "base_commit": plan.base_commit,
                    "plan_sha256": plan.plan_sha256,
                    "generated_at": plan.generated_at,
                    "work_dir": plan.work_dir,
                    "generator_model": plan.generator_model,
                    "dataset_revision": plan.dataset_revision,
                    "image_digest": plan.image_digest,
                    "base_validation_sha256": plan.base_validation_sha256,
                    "gold_validation_sha256": plan.gold_validation_sha256,
                    "commands": [
                        {
                            "argv": list(cmd.argv),
                            "timeout_seconds": cmd.timeout_seconds,
                            "required": cmd.required,
                            "cwd": cmd.cwd,
                        }
                        for cmd in plan.commands
                    ],
                }
            )
            validations.append(
                {
                    "instance_id": plan.instance_id,
                    "base_commit": plan.base_commit,
                    "plan_sha256": plan.plan_sha256,
                    "base": base_validation,
                    "gold": gold_validation,
                }
            )
            logger.info(
                "freeze %s digest=%s…",
                list(plan.commands[0].argv),
                digest[:24],
            )
    finally:
        if tmp.exists():
            for path in tmp.glob("*"):
                path.unlink(missing_ok=True)
            try:
                tmp.rmdir()
            except OSError:
                pass

    plan_set_sha256 = hashlib.sha256(
        "\n".join(str(plan["plan_sha256"]) for plan in frozen).encode("utf-8")
    ).hexdigest()
    document = {
        "plans": frozen,
        "meta": {
            "generator_model": _GENERATOR_MODEL,
            "dataset_revision": dataset_revision,
            "configuration_identity": "identical_plan_across_configurations",
            "validation": "base_and_gold_patched_container",
            "plan_set_sha256": plan_set_sha256,
        },
    }
    if failures:
        document["meta"]["failures"] = failures
        logger.error("regression plan freeze failed")
        for item in failures:
            logger.error("- %s", item)
        # Do not write a partial freeze; the study requires the full configured cohort.
        logger.error(
            "refusing to write %s: %d instance(s) failed",
            output,
            len(failures),
        )
        return 1

    validation_path = output.with_name("regression_plan_validation.json")
    _atomic_write(
        validation_path,
        json.dumps(
            {"plan_set_sha256": plan_set_sha256, "validations": validations},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    _atomic_write(
        output,
        yaml.safe_dump(document, sort_keys=False, default_flow_style=False),
    )
    logger.info("wrote %d plans -> %s", len(frozen), output)
    logger.info("wrote validation evidence -> %s", validation_path)
    return 0


def _atomic_write(path: Path, content: str) -> None:
    """Replace a generated evidence file without exposing a partial document."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--study",
        type=Path,
        required=True,
        help="Study directory containing dataset.yaml and regression_plans.yaml",
    )
    parser.add_argument(
        "--dataset-revision",
        default="SEC-bench/SEC-bench@eval",
        help="Frozen dataset identity recorded on every plan",
    )
    parser.add_argument(
        "--pull",
        action="store_true",
        help="docker pull missing images before validation",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: <study>/regression_plans.yaml)",
    )
    args = parser.parse_args(argv)
    study_dir = args.study if args.study.is_absolute() else get_repo_root() / args.study
    output = args.output or (study_dir / "regression_plans.yaml")
    return freeze_plans(
        study_dir=study_dir,
        dataset_revision=args.dataset_revision,
        pull=args.pull,
        output=output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
