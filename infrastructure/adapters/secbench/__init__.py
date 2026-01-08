"""SEC-bench Container Integration Module.

Self-contained module for SEC-bench evaluation with container lifecycle management.
Follows the plugin pattern - can be enabled/disabled without affecting core functionality.

Usage:
    from infrastructure.adapters.secbench import register_secbench, is_enabled

    if is_enabled(config):
        secbench_ctx = register_secbench(infrastructure, config)
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .config import SecBenchConfig, is_enabled
from .container_manager import ContainerManager
from .docker_adapter import DockerContainerAdapter
from .lifecycle_callback import ContainerLifecycleCallback, PromptBuilderProtocol, compose_callbacks
from .result_writer import PhaseResult, SecBenchResult, SecBenchResultWriter
from .verification_service import VerificationService

if TYPE_CHECKING:
    from core.ports.secbench_container_port import SecBenchContainerPort


@dataclass
class SecBenchContext:
    """Container for all SEC-bench components.

    Provides access to all SEC-bench services after registration.
    """

    container_port: "SecBenchContainerPort"
    container_manager: ContainerManager
    verification_service: VerificationService
    lifecycle_callback: ContainerLifecycleCallback
    result_writer: SecBenchResultWriter
    config: SecBenchConfig


def register_secbench(
    config: SecBenchConfig,
    prompt_builder: PromptBuilderProtocol | None = None,
    progress_callback: object | None = None,
) -> SecBenchContext:
    """Register all SEC-bench components.

    Creates and wires all SEC-bench services with proper dependency injection.

    Args:
        config: SEC-bench configuration.
        prompt_builder: Prompt builder to inject container_id into.
        progress_callback: Original progress callback to compose with.

    Returns:
        SecBenchContext with all registered components.
    """
    # Create container adapter (implements SecBenchContainerPort)
    container_adapter = DockerContainerAdapter(
        docker_socket=config.docker_socket_path,
    )

    # Create result writer
    result_writer = SecBenchResultWriter(
        output_directory=config.output_directory,
    )

    # Create verification service
    verification_service = VerificationService(
        container_port=container_adapter,
        timeout_seconds=config.default_timeout_seconds,
    )

    # Create container manager
    container_manager = ContainerManager(
        container_port=container_adapter,
        output_directory=config.output_directory,
        keep_running=config.keep_container_running,
    )

    # Create lifecycle callback
    lifecycle_callback = ContainerLifecycleCallback(
        container_manager=container_manager,
        verification_service=verification_service,
        result_writer=result_writer,
        prompt_builder=prompt_builder,
        progress_callback=progress_callback,
    )

    return SecBenchContext(
        container_port=container_adapter,
        container_manager=container_manager,
        verification_service=verification_service,
        lifecycle_callback=lifecycle_callback,
        result_writer=result_writer,
        config=config,
    )


def create_default_config(output_directory: str | Path = "output") -> SecBenchConfig:
    """Create default SEC-bench configuration.

    Args:
        output_directory: Directory for output files.

    Returns:
        SecBenchConfig with default values.
    """
    return SecBenchConfig(
        enabled=True,
        output_directory=Path(output_directory),
        keep_container_running=True,
        default_timeout_seconds=600,
    )


# Public API
__all__ = [
    # Configuration
    "SecBenchConfig",
    "is_enabled",
    "create_default_config",
    # Registration
    "register_secbench",
    "SecBenchContext",
    # Components
    "DockerContainerAdapter",
    "ContainerManager",
    "VerificationService",
    "ContainerLifecycleCallback",
    "SecBenchResultWriter",
    # Data types
    "SecBenchResult",
    "PhaseResult",
    # Utilities
    "compose_callbacks",
]
