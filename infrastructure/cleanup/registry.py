"""Per-process cleanup coordinator.

The :class:`CleanupRegistry` collects named cleanup callbacks and arranges
for them to fire exactly once on graceful termination — whether the
process exits normally (``atexit``) or receives ``SIGTERM`` / ``SIGINT``.
``SIGKILL`` is out of scope by definition; the matrix-startup sweep
handles cleanup of containers/files left behind by a hard-killed prior
run.

The module also exposes :func:`_pid_alive`, a small ``os.kill(pid, 0)``
wrapper used by the startup sweep (Phase 4 F.2) and by the MCP child
reaper (Phase 5 G.4). Both call sites import from one place to avoid the
circular-import gymnastics that would otherwise be required.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
from collections import OrderedDict
from collections.abc import Callable
from types import FrameType


logger = logging.getLogger(__name__)


class CleanupRegistry:
    """Ordered map of named cleanup callbacks fired on graceful exit.

    Handlers run in registration order. Each handler's exception is
    logged and swallowed so that one bad handler cannot block the
    others. After a successful sweep the registry empties itself, so a
    second invocation (for example, ``atexit`` firing after a signal
    handler already ran) is a no-op.
    """

    def __init__(self) -> None:
        self._handlers: OrderedDict[str, Callable[[], None]] = OrderedDict()
        self._installed: bool = False

    def register(self, name: str, handler: Callable[[], None]) -> None:
        """Register ``handler`` under ``name``; re-registering replaces."""
        # OrderedDict preserves the original insertion position on reassignment,
        # which matches the documented "register-then-replace" semantics.
        if name in self._handlers:
            self._handlers[name] = handler
            return
        self._handlers[name] = handler

    def install(
        self,
        *,
        signals: tuple[int, ...] = (signal.SIGTERM, signal.SIGINT),
    ) -> None:
        """Install signal handlers and an ``atexit`` hook firing :meth:`run_all`.

        Idempotent — repeat calls do not register duplicate signal
        handlers or duplicate ``atexit`` entries. Signal handlers call
        :meth:`run_all` and then re-raise the original signal via the
        default disposition so the process exits with the standard
        signal exit code.
        """
        if self._installed:
            return
        for sig in signals:
            signal.signal(sig, self._make_signal_handler(sig))
        atexit.register(self.run_all)
        self._installed = True

    def run_all(self) -> None:
        """Run every registered handler in registration order, then clear."""
        if not self._handlers:
            return
        for name, handler in list(self._handlers.items()):
            try:
                handler()
            except Exception:
                logger.warning(
                    "cleanup handler %r raised; continuing", name, exc_info=True
                )
        self._handlers.clear()

    def _make_signal_handler(
        self, sig: int
    ) -> Callable[[int, FrameType | None], None]:
        def _handler(signum: int, frame: FrameType | None) -> None:
            self.run_all()
            # Restore default disposition and re-raise so the process exits
            # with the conventional signal exit code (128 + signum).
            signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)

        return _handler


def _pid_alive(pid: int) -> bool:
    """Return whether ``pid`` corresponds to a process this kernel knows about.

    Uses the ``os.kill(pid, 0)`` "signal 0" probe — sends nothing but
    triggers the kernel's permission/existence checks. ``PermissionError``
    is treated as alive: an unknown-but-running process is safer to
    leave alone than to clean up.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
