"""Pull SEC-bench base images and build tool-enriched variants for the locked CVEs.

Uses the same resolution logic as the runtime (CVEInstance.docker_image +
resolve_secbench_image) to guarantee the images built here match what
run_experiment.py and DockerSecBenchRuntime expect at execution time.

Usage:
    uv run python -m experiments.setup_images
    uv run python -m experiments.setup_images --config experiments/configs/v3_b_rerun.yaml
    uv run python -m experiments.setup_images --dry-run
    uv run python -m experiments.setup_images --pull-only
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

import yaml

from plugins.security.cve_instance import CVEInstance
from plugins.security.image_resolver import resolve_secbench_image


logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "experiments" / "configs" / "locked_instances.yaml"
DOCKERFILE = REPO_ROOT / "deployment" / "secbench-tools.Dockerfile"


def _load_instances(config_path: Path) -> list[dict[str, str]]:
    """Load instance list from a locked-instances YAML config."""
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    instances = data.get("instances", [])
    if not instances:
        logger.error("No instances found in %s", config_path)
        sys.exit(1)
    return instances


def _image_exists(image: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
    )
    return result.returncode == 0


def _pull_image(image: str, *, dry_run: bool = False) -> bool:
    """Pull a Docker image. Returns True on success."""
    if dry_run:
        logger.info("[dry-run] would pull %s", image)
        return True
    logger.info("Pulling %s ...", image)
    result = subprocess.run(["docker", "pull", image])
    if result.returncode != 0:
        logger.error("Failed to pull %s", image)
        return False
    return True


def _build_tools_image(
    base_image: str, tools_image: str, *, dry_run: bool = False
) -> bool:
    """Build the secb-tools layer on top of a base image."""
    if dry_run:
        logger.info("[dry-run] would build %s from %s", tools_image, base_image)
        return True
    logger.info("Building %s from %s ...", tools_image, base_image)
    result = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(DOCKERFILE),
            "--build-arg",
            f"BASE_IMAGE={base_image}",
            "-t",
            tools_image,
            str(REPO_ROOT),
        ],
    )
    if result.returncode != 0:
        logger.error("Failed to build %s", tools_image)
        return False
    return True


def _resolve_images(instance: dict[str, str]) -> tuple[str, str]:
    """Resolve base and tools image names via the runtime's own logic.

    Loads a CVEInstance from the fixture JSON (via json_path in the config)
    to get docker_image, then applies resolve_secbench_image for the tools
    tag. Falls back to YAML fields when json_path is absent.
    """
    instance_id = instance["instance_id"]
    json_path = instance.get("json_path")

    if json_path:
        fixture = REPO_ROOT / json_path
        if fixture.exists():
            cve = CVEInstance.from_json_file(fixture)
            base_image = cve.docker_image
            tools_image = resolve_secbench_image(base_image, security_tools_enabled=True)
            return base_image, tools_image

    # Fallback: use explicit YAML fields (locked_instances.yaml has both)
    if "docker_image" in instance and "tools_image" in instance:
        return instance["docker_image"], instance["tools_image"]

    # Last resort: derive from instance_id
    parts = instance_id.split(".")
    project = parts[0]
    cve = ".".join(parts[1:]) if len(parts) > 1 else instance_id
    base_image = f"hwiwonlee/secb.eval.x86_64.{project}.{cve}"
    tools_image = resolve_secbench_image(base_image, security_tools_enabled=True)
    return base_image, tools_image


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Pull SEC-bench base images and build secb-tools variants.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to locked-instances YAML (default: experiments/configs/locked_instances.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without executing Docker commands",
    )
    parser.add_argument(
        "--pull-only",
        action="store_true",
        help="Only pull base images, skip building secb-tools layer",
    )
    args = parser.parse_args()

    if not args.config.exists():
        logger.error("Config not found: %s", args.config)
        return 1
    if not args.pull_only and not DOCKERFILE.exists():
        logger.error("Dockerfile not found: %s", DOCKERFILE)
        return 1

    instances = _load_instances(args.config)
    logger.info("Loaded %d instances from %s", len(instances), args.config)

    failed: list[str] = []

    for inst in instances:
        base_image, tools_image = _resolve_images(inst)
        instance_id = inst["instance_id"]

        if args.dry_run:
            action = "pull" if args.pull_only else "pull + build"
            logger.info(
                "[dry-run] %s: %s %s -> %s", instance_id, action, base_image, tools_image
            )
            continue

        if not args.pull_only and _image_exists(tools_image):
            logger.info("SKIP %s — %s already exists", instance_id, tools_image)
            continue

        if _image_exists(base_image):
            logger.info("SKIP pull %s — already present", base_image)
        elif not _pull_image(base_image):
            failed.append(instance_id)
            continue

        if args.pull_only:
            continue

        if not _build_tools_image(base_image, tools_image):
            failed.append(instance_id)

    if failed:
        logger.error("Failed instances: %s", ", ".join(failed))
        return 1

    logger.info("Done. All %d images ready.", len(instances))
    return 0


if __name__ == "__main__":
    sys.exit(main())
