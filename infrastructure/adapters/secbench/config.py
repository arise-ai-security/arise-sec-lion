"""Configuration for SEC-bench container integration module."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SecBenchConfig:
    """Configuration for SEC-bench container integration.

    Attributes:
        enabled: Whether SEC-bench container mode is enabled.
        output_directory: Directory for JSONL result files.
        keep_container_running: Keep container running after execution for manual verification.
        default_timeout_seconds: Default timeout for secb commands.
        docker_socket_path: Path to Docker socket for DooD pattern.
    """

    enabled: bool = False
    output_directory: Path = field(default_factory=lambda: Path("output"))
    keep_container_running: bool = True
    default_timeout_seconds: int = 600
    docker_socket_path: str = "/var/run/docker.sock"

    @classmethod
    def from_dict(cls, data: dict) -> "SecBenchConfig":
        """Create config from dictionary (e.g., from YAML)."""
        return cls(
            enabled=data.get("enabled", False),
            output_directory=Path(data.get("output_directory", "output")),
            keep_container_running=data.get("keep_container_running", True),
            default_timeout_seconds=data.get("default_timeout_seconds", 600),
            docker_socket_path=data.get("docker_socket_path", "/var/run/docker.sock"),
        )


def is_enabled(config: SecBenchConfig | None) -> bool:
    """Check if SEC-bench module is enabled."""
    return config is not None and config.enabled
