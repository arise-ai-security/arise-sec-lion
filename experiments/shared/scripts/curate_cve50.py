"""Curate the shared 50-instance CVE set for the N1/N2/B3/B4 cost studies.

Samples 50 instance ids from ``plugins/security/tests/fixtures`` with a fixed
seed so every study in the family references the identical task set, then
writes one ``dataset.yaml`` per study plus a lock file recording the seed,
selection order, and the 2-instance smoke pair.

The smoke pair is the first two instances in seeded selection order whose
``secb-tools:<instance>-patch`` Docker image already exists locally
(preferring a second instance from a different project for diversity), so the
smoke run needs no image builds.

Usage::

    uv run python -m experiments.shared.scripts.curate_cve50

Re-running is idempotent: identical seed and fixture set produce identical
outputs.
"""

from __future__ import annotations

import logging
import random
import shutil
import subprocess
from pathlib import Path

import yaml

from experiments.shared.scripts._paths import get_repo_root


logger = logging.getLogger(__name__)

SEED = 20260609
COUNT = 50
SMOKE_COUNT = 2
FIXTURES_DIR = Path("plugins/security/tests/fixtures")
LOCK_PATH = Path("experiments/shared/datasets/cve50-2026-06-09.lock.yaml")
STUDIES = (
    "n1-openhands-linear",
    "n2-openhands-subagents",
    "b3-boss-bef-direct",
    "b4-boss-manager-worker",
)


def _list_instance_ids(repo_root: Path) -> list[str]:
    fixtures = sorted((repo_root / FIXTURES_DIR).glob("*.json"))
    if len(fixtures) < COUNT:
        raise RuntimeError(
            f"need at least {COUNT} fixtures under {FIXTURES_DIR}, found {len(fixtures)}"
        )
    return [path.stem for path in fixtures]


def _image_exists(docker: str, instance_id: str) -> bool:
    result = subprocess.run(  # noqa: S603 - docker path resolved via shutil.which
        [docker, "image", "inspect", f"secb-tools:{instance_id}-patch"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    return result.returncode == 0


def _pick_smoke_pair(selection_order: list[str]) -> list[str]:
    """First two selected instances with local images, preferring distinct projects."""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("docker executable not found; cannot pick smoke instances")

    with_image = [iid for iid in selection_order if _image_exists(docker, iid)]
    if len(with_image) < SMOKE_COUNT:
        raise RuntimeError(
            f"only {len(with_image)} of the {COUNT} curated instances have local "
            "secb-tools images; build more via deployment/build-all-images.sh"
        )

    first = with_image[0]
    first_project = first.split(".", 1)[0]
    second = next(
        (iid for iid in with_image[1:] if iid.split(".", 1)[0] != first_project),
        with_image[1],
    )
    return [first, second]


def _write_dataset(study: str, instance_ids: list[str], repo_root: Path) -> Path:
    study_dir = repo_root / "experiments" / study
    study_dir.mkdir(parents=True, exist_ok=True)
    target = study_dir / "dataset.yaml"
    payload = {
        "name": f"{study}-cve50",
        "default_cves": instance_ids,
        "per_cell_overrides": {},
        "source": {
            "kind": "deployment-json",
            "paths": [f"{FIXTURES_DIR}/{iid}.json" for iid in instance_ids],
        },
    }
    target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return target


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo_root = get_repo_root()

    pool = _list_instance_ids(repo_root)
    rng = random.Random(SEED)
    selection_order = rng.sample(pool, COUNT)
    curated = sorted(selection_order)
    smoke_pair = _pick_smoke_pair(selection_order)

    lock_path = repo_root / LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_payload = {
        "seed": SEED,
        "count": COUNT,
        "fixture_pool_size": len(pool),
        "method": (
            "random.Random(seed).sample over sorted fixture stems; smoke pair = "
            "first two selected instances with local secb-tools images, second "
            "preferring a different project than the first"
        ),
        "selection_order": selection_order,
        "default_cves": curated,
        "smoke_tasks": smoke_pair,
        "studies": list(STUDIES),
    }
    lock_path.write_text(yaml.safe_dump(lock_payload, sort_keys=False), encoding="utf-8")
    logger.info("wrote %s", lock_path.relative_to(repo_root))

    for study in STUDIES:
        target = _write_dataset(study, curated, repo_root)
        logger.info("wrote %s", target.relative_to(repo_root))

    logger.info("smoke tasks: %s", ", ".join(smoke_pair))


if __name__ == "__main__":
    main()
