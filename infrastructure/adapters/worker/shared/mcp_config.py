"""Helpers for converting an engine-agnostic MCP server spec into engine shapes.

The MCP server itself is launched as a stdio subprocess. Each engine consumes a
slightly different config shape:

- Claude Code CLI: ``{"mcpServers": {<name>: {command, args, env}}}`` written
  to a JSON file passed via ``--mcp-config <path>``.
- Claude Agent SDK: a ``mcp_servers`` dict where each entry has
  ``type="stdio"`` plus ``command``/``args``/``env``.
- OpenHands SDK: a dict matching ``fastmcp.mcp_config.MCPConfig`` — same
  ``{"mcpServers": {...}}`` wrapper as the CLI.

The engine-agnostic inner spec may include an ``in_container`` override. That
metadata is consumed here and never forwarded to an MCP client.
"""

from __future__ import annotations

from typing import Any


_IN_CONTAINER_KEY = "in_container"


def _client_spec(spec: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(spec)
    cleaned.pop(_IN_CONTAINER_KEY, None)
    return cleaned


def to_cli_config_payload(servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"mcpServers": {name: _client_spec(spec) for name, spec in servers.items()}}


def to_sdk_mcp_servers(
    servers: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, spec in servers.items():
        entry: dict[str, Any] = {"type": "stdio"}
        entry.update(_client_spec(spec))
        out[name] = entry
    return out


def to_openhands_mcp_config(servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"mcpServers": {name: _client_spec(spec) for name, spec in servers.items()}}


def to_in_container_mcp_servers(
    servers: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Rewrite host stdio MCP specs so the server runs inside the container."""
    rewritten: dict[str, dict[str, Any]] = {}
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        entry = _client_spec(spec)
        override = spec.get(_IN_CONTAINER_KEY)
        if not isinstance(override, dict):
            rewritten[name] = entry
            continue
        if isinstance(override.get("command"), str):
            entry["command"] = override["command"]
        if isinstance(override.get("args"), list):
            entry["args"] = list(override["args"])
        remove_env = override.get("remove_env", [])
        removed = set(remove_env) if isinstance(remove_env, list) else set()
        new_env = {k: v for k, v in (entry.get("env") or {}).items() if k not in removed}
        override_env = override.get("env")
        if isinstance(override_env, dict):
            new_env.update(override_env)
        entry["env"] = new_env
        rewritten[name] = entry
    return rewritten
