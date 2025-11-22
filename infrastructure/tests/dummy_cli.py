#!/usr/bin/env python3
"""Dummy CLI tool for testing ClaudeCodePTYAdapter.

This script simulates Claude Code CLI behavior:
- Prints ANSI-colored output
- Outputs "thinking" patterns
- Sleeps to simulate processing
- Exits with success or failure based on input
"""

import sys
import time


def main():
    """Main entry point for dummy CLI."""
    # Print colored header (ANSI red)
    print("\x1b[31mDummy Claude Code CLI\x1b[0m")
    print("\x1b[32m> Starting task execution...\x1b[0m")

    # Read task description from command-line args or stdin
    if len(sys.argv) > 1:
        task_description = " ".join(sys.argv[1:])
    elif not sys.stdin.isatty():
        task_description = sys.stdin.readline().strip()
    else:
        task_description = ""

    # Simulate thinking process
    time.sleep(0.1)
    print("> Thinking about the task...")
    time.sleep(0.1)
    print("> Analyzing requirements...")
    time.sleep(0.1)
    print("> Planning the solution...")

    # Check if we should fail (for testing failure scenarios)
    if "fail" in task_description.lower():
        print("\x1b[31m> Error: Task failed!\x1b[0m")
        sys.exit(1)

    # Simulate successful completion
    time.sleep(0.1)
    print("> Executing the task...")
    time.sleep(0.1)
    print("\x1b[32m> Task completed successfully!\x1b[0m")
    print("Done")

    sys.exit(0)


if __name__ == "__main__":
    main()
