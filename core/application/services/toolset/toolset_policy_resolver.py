"""Resolve active toolsets for a role/domain pair."""

from __future__ import annotations

from typing import Any

from core.application.services.toolset.toolset_context import (
    ActiveToolContext,
    LoopPolicy,
    ToolsetPolicy,
)
from core.domain.values.enums import AgentRole
from core.ports.runtime_ports import Toolset


class ToolsetPolicyResolver:
    """Map role/domain config onto the registered toolsets."""

    def __init__(
        self,
        toolsets: list[Toolset] | tuple[Toolset, ...] = (),
        config: dict[str, Any] | None = None,
        default_loop_policy: LoopPolicy | None = None,
    ) -> None:
        self._toolsets = tuple(toolsets)
        self._config = config or {}
        self._default_loop_policy = default_loop_policy or LoopPolicy()

    def resolve(
        self,
        role: AgentRole,
        domain: object | None,
    ) -> ActiveToolContext:
        """Resolve the active tool definitions and execution map."""
        role_config = self._resolve_role_config(role.value.lower(), domain)
        loop_policy = LoopPolicy(
            max_iterations=int(
                role_config.get("max_iterations", self._default_loop_policy.max_iterations)
            ),
            result_char_limit=int(
                role_config.get(
                    "result_char_limit",
                    self._default_loop_policy.result_char_limit,
                )
            ),
        )

        toolset_configs = self._resolve_toolset_configs(role_config)
        tool_definitions: list[dict[str, Any]] = []
        executors: dict[str, Toolset] = {}

        for toolset in self._toolsets:
            policy = toolset_configs.get(toolset.name, ToolsetPolicy())
            if not policy.enabled:
                continue

            definitions = toolset.get_tool_definitions()
            if policy.allowed_tools is not None:
                definitions = [
                    tool_def
                    for tool_def in definitions
                    if tool_def.get("function", {}).get("name") in policy.allowed_tools
                ]

            for tool_def in definitions:
                tool_name = tool_def.get("function", {}).get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue

                existing_executor = executors.get(tool_name)
                if existing_executor is not None and existing_executor is not toolset:
                    raise ValueError(
                        f"Tool name collision for '{tool_name}' between "
                        f"'{existing_executor.name}' and '{toolset.name}'"
                    )

                tool_definitions.append(tool_def)
                executors[tool_name] = toolset

        return ActiveToolContext(
            tool_definitions=tuple(tool_definitions),
            loop_policy=loop_policy,
            executors=executors,
        )

    def _resolve_role_config(
        self,
        role_key: str,
        domain: object | None,
    ) -> dict[str, Any]:
        defaults = self._config.get("default", {})
        role_config = dict(defaults.get(role_key, {}))

        domain_key = self._domain_key(domain)
        if domain_key is None:
            return role_config

        domain_overrides = (
            self._config.get("domains", {}).get(domain_key, {}).get(role_key, {})
        )
        return self._merge_role_config(role_config, domain_overrides)

    @staticmethod
    def _merge_role_config(
        base: dict[str, Any],
        override: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(base)
        for key, value in override.items():
            if (
                key == "toolsets"
                and isinstance(merged.get(key), dict)
                and isinstance(value, dict)
            ):
                combined = dict(merged[key])
                for toolset_name, toolset_override in value.items():
                    existing = combined.get(toolset_name, {})
                    if isinstance(existing, dict) and isinstance(toolset_override, dict):
                        combined[toolset_name] = {**existing, **toolset_override}
                    else:
                        combined[toolset_name] = toolset_override
                merged[key] = combined
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _resolve_toolset_configs(
        role_config: dict[str, Any],
    ) -> dict[str, ToolsetPolicy]:
        toolset_configs: dict[str, Any] = {}

        if "enabled" in role_config or "allowed_tools" in role_config:
            toolset_configs["recon"] = {
                "enabled": role_config.get("enabled", True),
                "allowed_tools": role_config.get("allowed_tools"),
            }

        configured_toolsets = role_config.get("toolsets")
        if isinstance(configured_toolsets, dict):
            for toolset_name, toolset_config in configured_toolsets.items():
                if isinstance(toolset_config, dict):
                    base = toolset_configs.get(toolset_name, {})
                    toolset_configs[toolset_name] = {**base, **toolset_config}

        resolved: dict[str, ToolsetPolicy] = {}
        for toolset_name, toolset_config in toolset_configs.items():
            allowed = toolset_config.get("allowed_tools")
            resolved[toolset_name] = ToolsetPolicy(
                enabled=toolset_config.get("enabled", True),
                allowed_tools=frozenset(allowed) if allowed is not None else None,
            )
        return resolved

    @staticmethod
    def _domain_key(domain: object | None) -> str | None:
        if domain is None:
            return None
        if isinstance(domain, str):
            return domain

        type_name = type(domain).__name__.lower()
        if "cve" in type_name or "secbench" in type_name:
            return "secbench"
        return None
