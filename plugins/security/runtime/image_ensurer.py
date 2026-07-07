"""SEC-bench tools image acquisition: local inspect, registry pull and retag.

The ``timeout`` parameters are deliberate (ASYNC109 suppressed): they carry
the coordinator's configured per-command budget down to the ``docker``
inspect/tag calls, while image pulls use their own long-layer budget.
"""

from __future__ import annotations

import logging

from . import docker_cli


logger = logging.getLogger(__name__)

# Image pulls move multi-GB layers; give them far more headroom than the
# per-command default, which is sized for short docker exec/inspect calls.
_IMAGE_PULL_TIMEOUT_SECONDS = 1800.0


async def ensure_image_exists(image: str, *, registry: str, timeout: float) -> None:  # noqa: ASYNC109
    """Verify ``image`` exists locally, pulling from ``registry`` if configured.

    Raises:
        RuntimeError: if the image is absent and no registry pull recovers it.
    """
    exit_code, _, _ = await docker_cli.run_command(
        ["docker", "image", "inspect", image], timeout=timeout
    )
    if exit_code == 0:
        return
    if registry and await _pull_from_registry(image, registry=registry, timeout=timeout):
        return
    raise RuntimeError(
        f"Missing SEC-bench image {image}. Build it first with deployment/build-secbench-tools.sh."
    )


async def _pull_from_registry(image: str, *, registry: str, timeout: float) -> bool:  # noqa: ASYNC109
    """Pull a prebuilt tools image from the configured registry and alias it
    to the local tag, so a fresh machine runs without building.

    Best-effort: a pull or retag failure returns False, letting the caller
    fall back to the build-it-yourself error.
    """
    remote = f"{registry}/{image}"
    logger.info("Image %s not found locally; pulling %s", image, remote)
    pull_code, _, pull_err = await docker_cli.run_command(
        ["docker", "pull", remote], timeout=_IMAGE_PULL_TIMEOUT_SECONDS
    )
    if pull_code != 0:
        logger.warning("Pull of %s failed: %s", remote, pull_err.strip())
        return False
    tag_code, _, tag_err = await docker_cli.run_command(
        ["docker", "tag", remote, image], timeout=timeout
    )
    if tag_code != 0:
        logger.warning("Retag %s -> %s failed: %s", remote, image, tag_err.strip())
        return False
    return True
