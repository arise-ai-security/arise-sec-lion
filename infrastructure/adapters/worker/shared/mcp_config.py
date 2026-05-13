"""Helpers for converting an engine-agnostic MCP server spec into engine shapes.

The MCP server itself is launched as a stdio subprocess. Each engine consumes a
slightly different config shape:

- Claude Code CLI: ``{"mcpServers": {<name>: {command, args, env}}}`` written
  to a JSON file passed via ``--mcp-config <path>``.
- Claude Agent SDK: a ``mcp_servers`` dict where each entry has
  ``type="stdio"`` plus ``command``/``args``/``env``.
- OpenHands SDK: a dict matching ``fastmcp.mcp_config.MCPConfig`` — same
  ``{"mcpServers": {...}}`` wrapper as the CLI.

The "engine-agnostic" inner spec (``{"command": ..., "args": [...], "env": {...}}``)
is produced by ``plugins.security.mcp.security_tools_server.build_stdio_config``
and threaded through ``WorkspaceSpec.extras["mcp_servers"]`` /
``task_context["mcp_servers"]``.
"""

from __future__ import annotations

from typing import Any


def to_cli_config_payload(servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"mcpServers": dict(servers)}


def to_sdk_mcp_servers(
    servers: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, spec in servers.items():
        entry: dict[str, Any] = {"type": "stdio"}
        entry.update(spec)
        out[name] = entry
    return out


def to_openhands_mcp_config(servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"mcpServers": dict(servers)}
