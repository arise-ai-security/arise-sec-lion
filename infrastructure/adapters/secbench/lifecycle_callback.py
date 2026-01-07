"""Lifecycle callback for SEC-bench container events.

Hooks into agent lifecycle to automatically start/stop containers
for SEC-bench CVE evaluation runs.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from .container_manager import ContainerManager
from .result_writer import SecBenchResult, SecBenchResultWriter

if TYPE_CHECKING:
    from core.domain.values.cve_instance import CVEInstance
    from core.ports.secbench_container_port import ContainerInfo

    from .verification_service import VerificationService


class ContainerLifecycleCallback:
    """Progress callback for container lifecycle events.

    Implements the observer pattern to hook into agent lifecycle:
    - On BOSS creation: Start container if CVE instance present
    - On work completion: Run verification and write results
    - Print verification commands for manual inspection
    """

    def __init__(
        self,
        container_manager: ContainerManager,
        verification_service: "VerificationService",
        result_writer: SecBenchResultWriter,
        progress_callback: Any | None = None,
    ):
        """Initialize lifecycle callback.

        Args:
            container_manager: Manager for container operations.
            verification_service: Service for running secb commands.
            result_writer: Writer for JSONL results.
            progress_callback: Original progress callback to compose with.
        """
        self._manager = container_manager
        self._verification = verification_service
        self._writer = result_writer
        self._original_callback = progress_callback
        self._cve_instances: dict[UUID, "CVEInstance"] = {}

    async def on_boss_created(
        self,
        root_id: UUID,
        cve: "CVEInstance",
    ) -> "ContainerInfo":
        """Handle BOSS agent creation - start container.

        Args:
            root_id: Root agent ID (BOSS).
            cve: CVE instance for this run.

        Returns:
            ContainerInfo for the started container.
        """
        # Store CVE instance for later use
        self._cve_instances[root_id] = cve

        # Start container
        container_info = await self._manager.start_container_for_cve(cve, root_id)

        # Print verification commands
        print(self._manager.print_verification_commands(
            container_info,
            cve.instance_id,
        ))

        return container_info

    async def on_work_completed(
        self,
        root_id: UUID,
        run_verification: bool = True,
    ) -> SecBenchResult | None:
        """Handle work completion - optionally run verification and write results.

        Args:
            root_id: Root agent ID.
            run_verification: Whether to run secb verification commands.

        Returns:
            SecBenchResult if verification was run, None otherwise.
        """
        container_info = await self._manager.get_container_info(root_id)
        cve = self._cve_instances.get(root_id)

        if not container_info or not cve:
            return None

        result = SecBenchResult(
            instance_id=cve.instance_id,
            container_id=container_info.container_id,
        )

        if run_verification:
            # Run verification for each phase
            build_result = await self._verification.verify_build(
                container_info.container_id
            )
            result.builder = build_result.to_phase_result()

            # Only run repro if build passed
            if build_result.passed:
                repro_result = await self._verification.verify_repro(
                    container_info.container_id,
                    expected_error=cve.sanitizer_error if hasattr(cve, 'sanitizer_error') else None,
                )
                result.exploiter = repro_result.to_phase_result()

                # Only run patch if repro triggered sanitizer
                if repro_result.sanitizer_triggered:
                    patch_result = await self._verification.verify_patch(
                        container_info.container_id
                    )
                    result.fixer = patch_result.to_phase_result()

        # Write result to JSONL
        self._writer.write_result(result)

        # Print final status
        self._print_result_summary(result)

        return result

    async def on_work_failed(
        self,
        root_id: UUID,
        error: Exception,
    ) -> None:
        """Handle work failure - log error and optionally stop container.

        Args:
            root_id: Root agent ID.
            error: The error that caused failure.
        """
        cve = self._cve_instances.get(root_id)
        container_info = await self._manager.get_container_info(root_id)

        if cve and container_info:
            # Write failure result
            result = SecBenchResult(
                instance_id=cve.instance_id,
                container_id=container_info.container_id,
                metadata={"error": str(error)},
            )
            self._writer.write_result(result)

    def _print_result_summary(self, result: SecBenchResult) -> None:
        """Print result summary to console."""
        print("\n" + "=" * 60)
        print(f"SEC-bench Results: {result.instance_id}")
        print("=" * 60)

        if result.builder:
            status = "✓ PASSED" if result.builder.passed else "✗ FAILED"
            print(f"Builder:   {status}")

        if result.exploiter:
            status = "✓ PASSED" if result.exploiter.passed else "✗ FAILED"
            print(f"Exploiter: {status}")

        if result.fixer:
            status = "✓ PASSED" if result.fixer.passed else "✗ FAILED"
            print(f"Fixer:     {status}")

        print("=" * 60 + "\n")

    def get_container_id(self, root_id: UUID) -> str | None:
        """Get container ID for a root agent.

        Convenience method for prompt builders.

        Args:
            root_id: Root agent ID.

        Returns:
            Container ID if found.
        """
        return self._manager.get_container_id_from_context(root_id)


def compose_callbacks(
    secbench_callback: ContainerLifecycleCallback,
    original_callback: Any,
) -> Any:
    """Compose SEC-bench callback with original callback.

    Creates a wrapper that calls both callbacks.

    Args:
        secbench_callback: SEC-bench lifecycle callback.
        original_callback: Original progress callback.

    Returns:
        Composed callback function.
    """
    if original_callback is None:
        return secbench_callback

    async def composed_callback(event_type: str, data: dict) -> None:
        # Call original first
        if callable(original_callback):
            await original_callback(event_type, data)

        # Then SEC-bench callback based on event type
        if event_type == "boss_created" and "root_id" in data and "cve" in data:
            await secbench_callback.on_boss_created(
                root_id=data["root_id"],
                cve=data["cve"],
            )
        elif event_type == "work_completed" and "root_id" in data:
            await secbench_callback.on_work_completed(
                root_id=data["root_id"],
            )
        elif event_type == "work_failed" and "root_id" in data:
            await secbench_callback.on_work_failed(
                root_id=data["root_id"],
                error=data.get("error", Exception("Unknown error")),
            )

    return composed_callback
