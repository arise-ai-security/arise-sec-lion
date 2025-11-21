"""Bootstrap - Composition Root.

This module contains the main bootstrap function that wires all application
layers together using dependency injection. This is the heart of the
Composition Root pattern.

Reference: Mark Seemann - "Dependency Injection in .NET"
The bootstrap function is the single place where all object composition happens.
"""

from presentation.cli import CLI, CLIConfig

from .application import Application, ApplicationConfig, get_application
from .infrastructure import Infrastructure, InfrastructureConfig, get_infrastructure
from .presentation import get_cli


def bootstrap(
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
        infrastructure_config: Configuration for infrastructure adapters.
                              Uses defaults if not provided.
        application_config: Configuration for application services.
                           Uses defaults if not provided.
        cli_config: Configuration for CLI behavior.
                   Uses defaults if not provided.

    Returns:
        Fully wired CLI instance ready to run.

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
    # LAYER 1: Infrastructure - Adapters (implements domain ports)
    # ============================================================================
    # Infrastructure adapters implement the port interfaces defined by the domain.
    # They are concrete implementations that can be swapped without changing
    # domain or application logic.
    #
    # Dependencies: Infrastructure depends ONLY on port interfaces (inversion!)
    # ============================================================================

    if infrastructure_config is None:
        infrastructure_config = InfrastructureConfig(
            postgres_connection_string="postgresql://arise:arise@localhost:5432/arise_events",
            llm_model="gpt-4o-mini",
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
        application_config = ApplicationConfig(
            max_retries=3,
            poll_interval=0.5,
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
        cli_config = CLIConfig(verbose=True)

    cli: CLI = get_cli(application.execution_service, cli_config)

    return cli
