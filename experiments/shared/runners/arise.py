"""Default runner: 'arise'.

Every cell — flat or hierarchical — flows through the unified ``main.py run``
pipeline (via ``harness.run_arise``). The cell config selects between modes by
setting ``orchestration.mode: flat`` or ``orchestration.mode: hierarchical``;
``main.py`` reads the value through ``Settings`` and dispatches accordingly.

The previous PR-2 shim routed A-cells to ``harness.run_baseline`` to preserve
legacy behaviour. PR 4b removes that branch entirely now that flat-mode lives
inside the same pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from experiments.shared import harness
from experiments.shared.runners import register


if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID


class _Arise:
    """Default runner: dispatches every cell through ``harness.run_arise``."""

    id = "arise"
    label = "Our System"

    def run(
        self,
        *,
        study_id: str,
        cell: str,
        task: str,
        replicate: int,
        config: Path,
        context_file: Path,
    ) -> UUID:
        # ``context_file`` is unused: ``harness.run_arise`` derives the per-task
        # context_file from the dataset coverage map. We keep the parameter for
        # the runner-protocol shape; if a future runner needs to override the
        # fixture path it can do so without changing the protocol.
        del context_file
        return harness.run_arise(
            study_id=study_id,
            cell=cell,
            task=task,
            replicate=replicate,
            config=config,
        )

    async def run_async(
        self,
        *,
        study_id: str,
        cell: str,
        task: str,
        replicate: int,
        config: Path,
        context_file: Path,
    ) -> UUID:
        del context_file
        return await harness.run_arise_async(
            study_id=study_id,
            cell=cell,
            task=task,
            replicate=replicate,
            config=config,
        )


register(_Arise())
