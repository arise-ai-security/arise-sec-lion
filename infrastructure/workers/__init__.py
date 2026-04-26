"""Worker adapters conforming to ``core.ports.worker_port.WorkerPort``.

These adapters are the flat-mode counterpart to ``infrastructure/adapters/worker/``
(which streams events for hierarchical mode). A single task is dispatched per
``run_task`` call and the result is summarized in a frozen ``WorkerResult``.
"""

from __future__ import annotations

from .claude_code_worker import ClaudeCodeWorker
from .openhands_worker import OpenHandsWorker


__all__ = ["ClaudeCodeWorker", "OpenHandsWorker"]
