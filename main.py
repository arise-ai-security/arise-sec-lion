#!/usr/bin/env python3
"""Entry point for the Recursive Multi-Agent System.

Commands:
    run <task>  - Execute a task with the multi-agent system
    events      - View events for an agent run
    summary     - Show summary projection for a run
    list        - List past BOSS agent runs
"""

from bootstrap.bootstrap import main

if __name__ == "__main__":
    main()
