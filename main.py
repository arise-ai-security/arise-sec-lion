#!/usr/bin/env python3
"""Main entrypoint for the Recursive Multi-Agent System.

This is the application entry point. It delegates all dependency wiring
to the bootstrap layer and simply runs the composed application.

Separation of Concerns:
- main.py:        Entry point (runs the application)
- bootstrap.py:   Composition Root (wires dependencies)
- Other layers:   Business logic (domain, application, infrastructure, presentation)

Reference: Mark Seemann - "Dependency Injection in .NET"
The main function should be as thin as possible - just call bootstrap and run.
"""

import asyncio
import sys

from bootstrap import bootstrap


async def main() -> None:
    """Main entry point.

    Responsibilities:
    1. Call bootstrap() to wire all dependencies
    2. Run the composed application
    3. Handle interrupts gracefully

    All dependency wiring logic lives in bootstrap/bootstrap.py,
    keeping this entry point clean and focused.
    """
    # Wire all dependencies (Composition Root)
    app = bootstrap()

    # Run the application
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        print("⚠ Interrupted by user (Ctrl+C)")
        print("Note: Agent state is persisted in the event store.")
        print("You can resume by re-running with the same task.")
        sys.exit(0)
