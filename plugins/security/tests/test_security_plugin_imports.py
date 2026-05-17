import subprocess
import sys


def test_importing_security_package_does_not_preload_mcp_server() -> None:
    # Given: the MCP server is launched with ``python -m``, which first imports
    # ``plugins.security`` before executing the target module.
    script = (
        "import plugins.security, sys; "
        "raise SystemExit("
        "'plugins.security.mcp.security_tools_server' in sys.modules"
        ")"
    )

    # When: the security package is imported in a fresh interpreter.
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    # Then: the package import leaves the MCP server module unloaded, avoiding
    # runpy's "found in sys.modules" warning when the server starts via -m.
    assert result.returncode == 0, result.stderr or result.stdout
