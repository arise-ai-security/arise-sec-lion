"""Worker-prompt assembly.

Collects every ``PromptSent`` with ``prompt_type == "worker_execution"`` (the
final rendered prompt each worker received) and concatenates them with a labeled
header per worker. Events arrive time-ordered, so prompts reflect execution order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.domain.events.events import PromptSent
from experiments.shared.evaluation.common import (
    bracket_prefix,
    build_bef_phase_map,
    build_task_description_map,
    events_of_type,
)
from experiments.shared.evaluation.models import BefPhase, WorkerPrompt, WorkerPrompts


if TYPE_CHECKING:
    from experiments.shared.evaluation.models import RunData

WORKER_EXECUTION_PROMPT = "worker_execution"
_HEADER_RULE = "=" * 80


def collect_worker_prompts(run_data: RunData) -> list[WorkerPrompt]:
    """All worker-execution prompts for the run, in execution (time) order."""
    phase_map = build_bef_phase_map(run_data.events, run_data.run_id)
    task_map = build_task_description_map(run_data.events)

    collected: list[WorkerPrompt] = []
    for event in events_of_type(run_data.events, PromptSent):
        if event.prompt_type != WORKER_EXECUTION_PROMPT:
            continue
        collected.append(
            WorkerPrompt(
                agent_id=event.aggregate_id,
                phase=phase_map.get(event.aggregate_id, BefPhase.ORCHESTRATION),
                role_label=bracket_prefix(task_map.get(event.aggregate_id)) or "-",
                sequence_number=event.sequence_number,
                target=event.target,
                prompt=event.prompt,
            )
        )
    return collected


def _prettify(prompts: list[WorkerPrompt]) -> str:
    blocks: list[str] = []
    for index, wp in enumerate(prompts, start=1):
        header = (
            f"{_HEADER_RULE}\n"
            f"[{index}/{len(prompts)}] {wp.phase.value} / {wp.role_label} — "
            f"agent {wp.agent_id} (seq {wp.sequence_number}, target={wp.target})\n"
            f"{_HEADER_RULE}"
        )
        blocks.append(f"{header}\n{wp.prompt}")
    return "\n\n".join(blocks)


def worker_prompts(run_data: RunData) -> WorkerPrompts:
    """Worker-execution prompts plus a prettified, labeled concatenation."""
    collected = collect_worker_prompts(run_data)
    return WorkerPrompts(prompts=collected, text=_prettify(collected))
