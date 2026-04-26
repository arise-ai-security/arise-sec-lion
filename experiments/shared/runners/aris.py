"""Default runner: 'aris'. PR 2 implementation is a thin shim delegating
to the existing `harness.run_ours` / `harness.run_baseline` functions. PR 4b
will rewrite it to:
  - Resolve the cell config overlay via `Settings.from_yaml`.
  - Build invariants via `core.application.run_invariants`.
  - Dispatch on `settings.orchestration.mode` (flat | hierarchical).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from experiments.shared import harness
from experiments.shared.runners import register


if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID


# Cell-name -> harness baseline variant. Temporary mapping retained only so PR 2
# can dispatch A-cells to `harness.run_baseline` without changing semantics. PR
# 4b deletes this entirely once flat-mode dispatch reads `orchestration.mode:
# flat` + `worker.tool: claude_code` directly from the resolved config.
_LEGACY_VARIANTS: dict[str, str] = {
    "A1": "claude-code-subagent",
    "A2": "claude-code-nosubagent",
}


class _Aris:
    """Default runner: dispatches A-cells to `run_baseline` and B-cells to `run_ours`."""

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
        if cell.startswith("A"):
            variant = _LEGACY_VARIANTS.get(cell)
            if variant is None:
                raise ValueError(
                    f"cell {cell!r} starts with 'A' but has no legacy baseline variant; "
                    f"add it to _LEGACY_VARIANTS or wait for PR 4b."
                )
            return harness.run_baseline(
                study_id=study_id,
                cell=cell,
                task=task,
                attempt=replicate,
                variant=variant,
            )
        return harness.run_ours(
            study_id=study_id,
            cell=cell,
            task=task,
            attempt=replicate,
            config=config,
        )


register(_Aris())
