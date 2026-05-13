"""Output rendering for CLI display (SRP compliant)."""



import click


class OutputRenderer:
    """Handles all CLI output formatting and display.

    Single Responsibility: Only handles display/presentation logic.
    """

    @classmethod
    def print_banner(cls) -> None:
        click.echo("╔═══════════════════════════════════════════════════════════════════╗")
        click.echo("║  Recursive Multi-Agent System - Powered by Event Sourcing         ║")
        click.echo("╚═══════════════════════════════════════════════════════════════════╝")
        click.echo()

    @classmethod
    def print_result_header(cls) -> None:
        click.echo()
        click.echo("╔═══════════════════════════════════════════════════════════════════╗")
        click.echo("║  FINAL RESULT                                                    ║")
        click.echo("╚═══════════════════════════════════════════════════════════════════╝")
        click.echo()

    @classmethod
    def print_step(cls, step: int, total: int, message: str) -> None:
        click.echo(f"[{step}/{total}] {message}")

    @classmethod
    def print_success(cls, message: str) -> None:
        click.echo(f"   ✓ {message}")

    @classmethod
    def print_error(cls, message: str) -> None:
        click.echo(f"   ✗ {message}")

    @classmethod
    def print_info(cls, message: str) -> None:
        click.echo(f"   {message}")

    @classmethod
    def print_runs_table(cls, runs: list[dict]) -> None:
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
        cls.print_error(f"Failed to connect to database: {error}")
        click.echo()
        click.echo("Make sure PostgreSQL is running:")
        click.echo("  docker compose up -d postgres")
        click.echo()

    @classmethod
    def print_shutdown(cls) -> None:
        click.echo("✓ System shutdown complete")
