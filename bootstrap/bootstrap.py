"""Bootstrap - Composition Root: wires all layers via dependency injection."""

from pathlib import Path

from config import Settings
from presentation.cli import CLI, CLIConfig, format_event_progress, format_status_display

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
            postgres_connection_string=settings.database.connection_string,
            default_worker_tool=settings.worker.tool_type,
            worker_tool_model=settings.worker.tool_model,
            worker_tool_timeout=settings.worker.tool_timeout,
        )

    infrastructure: Infrastructure = get_infrastructure(infrastructure_config)

    if application_config is None:
        model_config = {"boss": settings.llm.model_boss}
        application_config = ApplicationConfig(
            system_limits=settings.orchestration.limits,
            max_retries=settings.orchestration.max_retries,
            poll_interval=settings.orchestration.poll_interval,
            model_config=model_config,
            output_directory=settings.output.directory,
            default_worker_tool=settings.worker.tool_type,
            budget_max_total_cost_usd=settings.orchestration.budget.max_total_cost_usd,
            budget_cost_warning_threshold=settings.orchestration.budget.cost_warning_threshold,
            budget_cost_tracking_enabled=settings.orchestration.budget.cost_tracking_enabled,
            progress_callback=format_event_progress if settings.output.verbose else None,
            status_callback=format_status_display if settings.output.verbose else None,
        )

    application: Application = get_application(infrastructure, application_config)

    if cli_config is None:
        cli_config = CLIConfig(
            verbose=settings.output.verbose,
            output_directory=settings.output.directory,
        )

    cli: CLI = get_cli(application.execution_service, cli_config)

    return cli
