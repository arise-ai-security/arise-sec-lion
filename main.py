#!/usr/bin/env python3
"""Main entrypoint for the Recursive Multi-Agent System.

This is the application entry point. It uses Click for CLI commands:
    run <task>     - Execute a task with the multi-agent system
    events         - View events for an agent run
    summary        - Show summary projection for a run
    list           - List past BOSS agent runs

Separation of Concerns:
- main.py:        Entry point (runs the CLI)
- cli.py:         Click commands (handles user interaction)
- bootstrap.py:   Composition Root (wires dependencies)
- Other layers:   Business logic (domain, application, infrastructure, presentation)

Reference: Mark Seemann - "Dependency Injection in .NET"
The main function should be as thin as possible - just call the CLI.
"""

import sys

from presentation.cli import cli


def main() -> None:
    """Main entry point.

    Responsibilities:
    1. Invoke the Click CLI
    2. Handle interrupts gracefully

    All command handling is done by Click in presentation/cli.py,
    keeping this entry point clean and focused.
    """
    try:
        cli()
    except KeyboardInterrupt:
        print()
        print("⚠ Interrupted by user (Ctrl+C)")
        print("Note: Agent state is persisted in the event store.")
        print("You can resume by re-running with the same task.")
        sys.exit(0)


if __name__ == "__main__":
    main()
