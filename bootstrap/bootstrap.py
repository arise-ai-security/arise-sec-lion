"""Bootstrap - Composition Root.

This module contains the main bootstrap function that wires all application
layers together using dependency injection. This is the heart of the
Composition Root pattern.

Reference: Mark Seemann - "Dependency Injection in .NET"
The bootstrap function is the single place where all object composition happens.
"""

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
    """Bootstrap the application by wiring all dependencies.

    This is the Composition Root - the single function responsible for
    composing the entire object graph. It instantiates and wires together:
    1. Infrastructure adapters (event store, LLM, worker tools)
    2. Application services (execution service)
    3. Presentation layer (CLI)

    Args:
        config_path: Path to YAML configuration file. If provided, loads
                    Settings from the file and uses them to build config objects.
                    Environment variables still take precedence for secrets.
        infrastructure_config: Configuration for infrastructure adapters.
                              Overrides settings from config_path if provided.
                              Uses defaults if neither is provided.
        application_config: Configuration for application services.
                           Overrides settings from config_path if provided.
                           Uses defaults if neither is provided.
        cli_config: Configuration for CLI behavior.
                   Overrides settings from config_path if provided.
                   Uses defaults if neither is provided.

    Returns:
        Fully wired CLI instance ready to run.

    Configuration Precedence:
        1. Explicitly passed config objects (infrastructure_config, etc.)
        2. Config file (config_path)
        3. Environment variables (for secrets)
        4. Defaults (in code)

    Architecture Note:
        This function knows about ALL layers and composes them together.
        The dependency flow is:
          Domain ← Application ← Infrastructure
                     ↑
                 Presentation
                     ↑
                 Bootstrap (this function)

        Each layer depends only on inner layers, never on outer layers.
        Bootstrap is the outermost layer that knows about everything.
    """

    # ============================================================================
    # CONFIGURATION LOADING
    # ============================================================================
    # Configuration cascade (highest to lowest precedence):
    #   1. CLI args (config_path parameter)
    #   2. Environment variables (ARISE_INFRA_*, ARISE_APP_*, etc.)
    #   3. Environment-specific config (config/{ARISE_ENV}.yaml)
    #   4. Default config (config/default.yaml)
    #   5. Code defaults (in settings.py)
    #
    # Set ARISE_ENV to control which environment config is loaded:
    #   - "development" (default): Uses config/development.yaml
    #   - "production": Uses config/production.yaml
    #   - "test": Uses config/test.yaml
    # ============================================================================

    # Load settings: explicit config file OR cascade (env.yaml -> default.yaml -> code defaults)
    settings = Settings.from_yaml(config_path) if config_path is not None else Settings.load()

    # ============================================================================
    # LAYER 1: Infrastructure - Adapters (implements domain ports)
    # ============================================================================
    # Infrastructure adapters implement the port interfaces defined by the domain.
    # They are concrete implementations that can be swapped without changing
    # domain or application logic.
    #
    # Dependencies: Infrastructure depends ONLY on port interfaces (inversion!)
    # ============================================================================

    if infrastructure_config is None:
        # Build from settings (loaded from config file or environment variables)
        # All fields are required - if not configured, InfrastructureConfig raises TypeError
        infrastructure_config = InfrastructureConfig(
            postgres_connection_string=settings.infrastructure.postgres_connection_string,
            default_worker_tool=settings.infrastructure.worker_tool_type,
            worker_tool_model=settings.infrastructure.worker_tool_model,
            worker_tool_timeout=settings.infrastructure.worker_tool_timeout,
        )

    infrastructure: Infrastructure = get_infrastructure(infrastructure_config)

    # ============================================================================
    # LAYER 2: Application - Services (orchestrates domain via ports)
    # ============================================================================
    # Application services coordinate domain logic using infrastructure adapters
    # through port interfaces. They implement business workflows (use cases).
    #
    # Dependencies: Application depends on Infrastructure (via port interfaces)
    # ============================================================================

    if application_config is None:
        # Build model_config from infrastructure settings
        model_config = {
            "boss": settings.infrastructure.llm_model_boss,
            "manager": settings.infrastructure.llm_model_manager,
            "worker": settings.infrastructure.llm_model_worker,
            "pending": settings.infrastructure.llm_model_pending,
        }

        # Build from settings (loaded from config file or environment variables)
        # Note: working_directory comes from presentation settings (output_directory)
        # where worker tools should write generated code
        # Progress callback enables real-time event output during orchestration
        # default_worker_tool guides LLM to specify the correct tool in subtask configs
        application_config = ApplicationConfig(
            max_retries=settings.application.max_retries,
            poll_interval=settings.application.poll_interval,
            model_config=model_config,
            working_directory=settings.presentation.output_directory,
            progress_callback=format_event_progress if settings.presentation.verbose else None,
            default_worker_tool=settings.infrastructure.worker_tool_type,
        )

    application: Application = get_application(infrastructure, application_config)

    # ============================================================================
    # LAYER 3: Presentation - CLI (user interface)
    # ============================================================================
    # The presentation layer handles user interaction and delegates to the
    # application layer for business logic execution.
    #
    # Dependencies: Presentation → Application (execution_service)
    #               Presentation does NOT depend on Bootstrap!
    # ============================================================================

    if cli_config is None:
        # Build from settings (loaded from config file or environment variables)
        cli_config = CLIConfig(
            verbose=settings.presentation.verbose,
            output_directory=settings.presentation.output_directory,
        )

    cli: CLI = get_cli(application.execution_service, cli_config)

    return cli
