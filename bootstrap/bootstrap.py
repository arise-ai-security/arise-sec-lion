"""Bootstrap - Composition Root: wires all layers via dependency injection."""

from pathlib import Path

from config import Settings
from presentation.cli import CLI, CLIConfig, format_event_progress

from .application import Application, ApplicationConfig, get_application
from .infrastructure import Infrastructure, InfrastructureConfig, get_infrastructure
from .presentation import get_cli


def bootstrap(
    config_path: str | Path | None = None,
    infrastructure_config: InfrastructureConfig | None = None,
    application_config: ApplicationConfig | None = None,
    cli_config: CLIConfig | None = None,
) -> CLI:
    """Wire Infrastructure → Application → Presentation. Returns ready CLI."""
    # Only load settings if we need them (i.e., some config is not provided)
    settings = None
    if infrastructure_config is None or application_config is None or cli_config is None:
        settings = Settings.from_yaml(config_path) if config_path is not None else Settings.load()

    if infrastructure_config is None:
        infrastructure_config = InfrastructureConfig(
            postgres_connection_string=settings.postgres_connection_string,
            default_worker_tool=settings.infrastructure.worker_tool_type,
            worker_tool_model=settings.infrastructure.worker_tool_model,
            worker_tool_timeout=settings.infrastructure.worker_tool_timeout,
        )

    infrastructure: Infrastructure = get_infrastructure(infrastructure_config)

    if application_config is None:
        model_config = {"boss": settings.infrastructure.llm_model_boss}
        application_config = ApplicationConfig(
            max_retries=settings.application.max_retries,
            poll_interval=settings.application.poll_interval,
            model_config=model_config,
            output_directory=settings.presentation.output_directory,
            progress_callback=format_event_progress if settings.presentation.verbose else None,
            default_worker_tool=settings.infrastructure.worker_tool_type,
        )

    application: Application = get_application(infrastructure, application_config)

    if cli_config is None:
        cli_config = CLIConfig(
            verbose=settings.presentation.verbose,
            output_directory=settings.presentation.output_directory,
        )

    cli: CLI = get_cli(application.execution_service, cli_config)

    return cli
