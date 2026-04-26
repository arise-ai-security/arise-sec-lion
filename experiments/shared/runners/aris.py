"""Default runner: 'aris'.

Every cell — flat or hierarchical — flows through the unified ``main.py run``
pipeline (via ``harness.run_ours``). The cell config selects between modes by
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


class _Aris:
    """Default runner: dispatches every cell through ``harness.run_ours``."""

    id = "aris"
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
        # ``context_file`` is unused: ``harness.run_ours`` derives the per-task
        # context_file from the dataset coverage map. We keep the parameter for
        # the runner-protocol shape; if a future runner needs to override the
        # fixture path it can do so without changing the protocol.
        del context_file
        return harness.run_ours(
            study_id=study_id,
            cell=cell,
            task=task,
            attempt=replicate,
            config=config,
        )


register(_Aris())
