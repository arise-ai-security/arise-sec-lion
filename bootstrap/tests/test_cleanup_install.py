"""Tests for F.1 cleanup-registry installation at bootstrap entry.

Verifies that :func:`bootstrap.bootstrap.main` instantiates a
``CleanupRegistry`` and calls :meth:`install` before dispatching to a
command handler, and that the registry is threaded into the dispatched
handler.

Signal handlers are NOT actually installed in pytest's process because
:meth:`CleanupRegistry.install` is patched to a no-op for the duration of
each test. ``asyncio.run`` is also patched so no real command coroutine
runs.
"""

from __future__ import annotations

import signal
from unittest.mock import MagicMock, patch

import pytest


def _stub_async_run(coro):  # noqa: ANN001 - test helper
    """Close the coroutine and return None — stand-in for ``asyncio.run``."""
    coro.close()
    return None


def test_main_installs_cleanup_registry_with_sigterm_and_sigint() -> None:
    """``main()`` installs the cleanup registry for SIGTERM + SIGINT."""

    # Given: the run handler returns a benign coroutine and CleanupRegistry
    #        is patched so we can observe install() without touching the
    #        pytest process's signal handlers.
    from bootstrap import bootstrap as bootstrap_module

    install_calls: list[tuple[int, ...]] = []

    class _FakeRegistry:
        def __init__(self) -> None:
            self._installed = False

        def install(self, *, signals: tuple[int, ...]) -> None:
            install_calls.append(signals)
            self._installed = True

        def register(self, name: str, handler) -> None:  # noqa: ANN001 - test stub
            del name, handler

        def run_all(self) -> None:
            return None

    async def _fake_run_task(args, *, cleanup_registry) -> None:  # noqa: ANN001 - test stub
        del args, cleanup_registry

    # When: main() runs with a "list" command (cheapest path; no Postgres).
    with (
        patch.object(bootstrap_module, "CleanupRegistry", _FakeRegistry),
        patch.object(bootstrap_module, "_run_task", _fake_run_task),
        patch.object(bootstrap_module, "asyncio") as mock_asyncio,
    ):
        mock_asyncio.run.side_effect = _stub_async_run
        bootstrap_module.main(["run", "smoke-task"])

    # Then: install() was called exactly once with the expected signal set.
    assert install_calls == [(signal.SIGTERM, signal.SIGINT)]


def test_main_threads_registry_into_dispatched_handler() -> None:
    """``main()`` passes the same registry instance to the command handler."""

    # Given: a recording fake registry and a recording fake handler.
    from bootstrap import bootstrap as bootstrap_module

    forwarded: dict[str, object] = {}

    class _FakeRegistry:
        def install(self, *, signals: tuple[int, ...]) -> None:  # noqa: ARG002 - signature parity
            return None

        def register(self, name: str, handler) -> None:  # noqa: ANN001 - test stub
            del name, handler

        def run_all(self) -> None:
            return None

    async def _fake_run_task(args, *, cleanup_registry) -> None:  # noqa: ANN001 - test stub
        forwarded["args"] = args
        forwarded["cleanup_registry"] = cleanup_registry

    def _run_to_completion(coro):  # noqa: ANN001 - test helper
        try:
            while True:
                coro.send(None)
        except StopIteration:
            return None

    with (
        patch.object(bootstrap_module, "CleanupRegistry", _FakeRegistry),
        patch.object(bootstrap_module, "_run_task", _fake_run_task),
        patch.object(bootstrap_module, "asyncio") as mock_asyncio,
    ):
        mock_asyncio.run.side_effect = _run_to_completion
        bootstrap_module.main(["run", "smoke-task"])

    # Then: the handler received exactly the registry main() created.
    assert isinstance(forwarded["cleanup_registry"], _FakeRegistry)


def test_main_runs_cleanup_on_keyboardinterrupt_backstop() -> None:
    """A ``KeyboardInterrupt`` escaping ``asyncio.run`` triggers ``run_all``."""

    # Given: a handler whose coroutine raises KeyboardInterrupt synchronously.
    from bootstrap import bootstrap as bootstrap_module

    run_all_calls: list[bool] = []

    class _FakeRegistry:
        def install(self, *, signals: tuple[int, ...]) -> None:  # noqa: ARG002 - signature parity
            return None

        def register(self, name: str, handler) -> None:  # noqa: ANN001 - test stub
            del name, handler

        def run_all(self) -> None:
            run_all_calls.append(True)

    async def _interrupted_run_task(args, *, cleanup_registry) -> None:  # noqa: ANN001 - test stub
        del args, cleanup_registry
        raise KeyboardInterrupt

    def _real_async_run(coro):  # noqa: ANN001 - test helper
        try:
            coro.send(None)
        except StopIteration:
            return None
        except KeyboardInterrupt:
            raise
        coro.close()
        return None

    # When: main() runs and KeyboardInterrupt propagates out of asyncio.run.
    with (
        patch.object(bootstrap_module, "CleanupRegistry", _FakeRegistry),
        patch.object(bootstrap_module, "_run_task", _interrupted_run_task),
        patch.object(bootstrap_module, "asyncio") as mock_asyncio,
        patch.object(bootstrap_module, "print") as _mock_print,
    ):
        mock_asyncio.run.side_effect = _real_async_run
        with pytest.raises(SystemExit) as exc_info:
            bootstrap_module.main(["run", "smoke-task"])

    # Then: SystemExit(0) AND run_all was invoked as the defensive backstop.
    assert exc_info.value.code == 0
    assert run_all_calls == [True]


def test_main_real_registry_install_called_once() -> None:
    """The real ``CleanupRegistry.install`` is called exactly once.

    Patches the bound method at the class level so the real instance
    method is replaced; this also avoids touching the pytest process's
    signal handlers.
    """

    from bootstrap import bootstrap as bootstrap_module

    async def _noop_run_task(args, *, cleanup_registry) -> None:  # noqa: ANN001 - test stub
        del args, cleanup_registry

    with (
        patch(
            "bootstrap.bootstrap.CleanupRegistry.install",
            MagicMock(),
        ) as patched_install,
        patch.object(bootstrap_module, "_run_task", _noop_run_task),
        patch.object(bootstrap_module, "asyncio") as mock_asyncio,
    ):
        mock_asyncio.run.side_effect = _stub_async_run
        bootstrap_module.main(["run", "smoke-task"])

        # Then: install was called exactly once with the documented signal set.
        patched_install.assert_called_once_with(
            signals=(signal.SIGTERM, signal.SIGINT)
        )
