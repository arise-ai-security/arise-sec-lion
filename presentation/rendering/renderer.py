"""Output rendering for CLI display (SRP compliant)."""



import click


class OutputRenderer:
    """Handles all CLI output formatting and display.

    Single Responsibility: Only handles display/presentation logic.
    """

    BANNER_WIDTH = 69

    @classmethod
    def print_banner(cls) -> None:
        """Print application banner."""
        click.echo("╔═══════════════════════════════════════════════════════════════════╗")
        click.echo("║  Recursive Multi-Agent System - Powered by Event Sourcing         ║")
        click.echo("╚═══════════════════════════════════════════════════════════════════╝")
        click.echo()

    @classmethod
    def print_result_header(cls) -> None:
        """Print final result header."""
        click.echo()
        click.echo("╔═══════════════════════════════════════════════════════════════════╗")
        click.echo("║  FINAL RESULT                                                    ║")
        click.echo("╚═══════════════════════════════════════════════════════════════════╝")
        click.echo()

    @classmethod
    def print_usage(cls) -> None:
        """Print CLI usage instructions."""
        cls.print_banner()
        click.echo("ERROR: No task provided!")
        click.echo()
        click.echo("Usage:")
        click.echo('  python main.py run "<your task description>"')
        click.echo()
        click.echo("Examples:")
        click.echo('  python main.py run "Build a REST API with authentication"')
        click.echo('  python main.py run "Analyze this codebase and summarize its architecture"')
        click.echo('  python main.py run "Refactor the payment module to use Strategy pattern"')
        click.echo()
        click.echo("Other commands:")
        click.echo("  python main.py events    - View events for last run")
        click.echo("  python main.py summary   - Show summary projection")
        click.echo("  python main.py list      - List past BOSS agent runs")
        click.echo()

    @classmethod
    def print_step(cls, step: int, total: int, message: str) -> None:
        """Print a numbered step message."""
        click.echo(f"[{step}/{total}] {message}")

    @classmethod
    def print_success(cls, message: str) -> None:
        """Print a success message with checkmark."""
        click.echo(f"   ✓ {message}")

    @classmethod
    def print_error(cls, message: str) -> None:
        """Print an error message with X."""
        click.echo(f"   ✗ {message}")

    @classmethod
    def print_info(cls, message: str) -> None:
        """Print an info message."""
        click.echo(f"   {message}")

    @classmethod
    def print_runs_table(cls, runs: list[dict]) -> None:
        """Print a table of BOSS runs."""
        if not runs:
            click.echo("No BOSS runs found.")
            return

        click.echo()
        click.echo(f"{'BOSS ID':<36} | {'Started':<20} | {'Events':<6} | Status")
        click.echo("-" * 80)
        for r in runs:
            click.echo(
                f"{r['boss_id']} | {r['started_at'][:19]} | {r['event_count']:<6} | {r['status']}"
            )
        click.echo()

    @classmethod
    def print_final_result(
        cls,
        result: str | None,
        status: str,
        total_agents: int,
    ) -> None:
        """Print the final execution result."""
        cls.print_result_header()

        if result:
            click.echo(result)
        else:
            click.echo("(No result produced)")

        click.echo()
        click.echo(f"Status: {status}")
        click.echo()
        click.echo(f"Total Agents Created: {total_agents}")
        click.echo()

    @classmethod
    def print_database_error(cls, error: Exception) -> None:
        """Print database connection error with guidance."""
        cls.print_error(f"Failed to connect to database: {error}")
        click.echo()
        click.echo("Make sure PostgreSQL is running:")
        click.echo("  docker compose up -d postgres")
        click.echo()

    @classmethod
    def print_shutdown(cls) -> None:
        """Print shutdown complete message."""
        click.echo("✓ System shutdown complete")
