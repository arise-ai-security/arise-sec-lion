"""Runner registry: dispatch a study's cell to the right runtime.

Today there's exactly one runner -- `aris` -- which delegates to the existing
`harness.run_ours` and `harness.run_baseline` functions. Future runners
(external research baselines that can't be expressed as a config of our
system) plug in by registering a Runner instance at module import.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING, Protocol, runtime_checkable


if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID


logger = logging.getLogger(__name__)


@runtime_checkable
class Runner(Protocol):
    """A runtime that can execute one cell of a study."""

    id: str
    label: str

    def run(
        self,
        *,
        study_id: str,
        cell: str,
        task: str,
        replicate: int,
        config: Path,
        context_file: Path,
    ) -> UUID: ...


_REGISTRY: dict[str, Runner] = {}


def register(runner: Runner) -> Runner:
    """Register a runner instance; raise on id collision."""
    if runner.id in _REGISTRY:
        existing = type(_REGISTRY[runner.id]).__module__
        raise ValueError(f"runner id collision: {runner.id!r} already registered by {existing}")
    _REGISTRY[runner.id] = runner
    return runner


def get(runner_id: str) -> Runner:
    """Return the runner registered under `runner_id` or raise KeyError."""
    try:
        return _REGISTRY[runner_id]
    except KeyError:
        raise KeyError(f"unknown runner {runner_id!r}; registered: {sorted(_REGISTRY)}") from None


def all_ids() -> list[str]:
    """Return sorted ids of every registered runner."""
    return sorted(_REGISTRY)


# Auto-discover runner modules in this package. Each module is responsible for
# calling `register(...)` at import time on the runner instance(s) it defines.
for _module_info in pkgutil.iter_modules(__path__):
    if _module_info.name.startswith("_"):
        continue
    try:
        importlib.import_module(f"{__name__}.{_module_info.name}")
    except ImportError as exc:
        logger.warning("skipping runner module %s: %s", _module_info.name, exc)
