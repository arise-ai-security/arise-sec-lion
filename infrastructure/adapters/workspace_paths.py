"""Workspace path alias mapping shared by infrastructure adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.ports.domain_plugin_port import WorkspacePathAlias


@dataclass(frozen=True, slots=True)
class WorkspacePathMapper:
    """Maps agent-facing workspace paths to their host mirror paths."""

    aliases: tuple[WorkspacePathAlias, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", self._normalize_aliases(self.aliases))

    @staticmethod
    def _normalize_aliases(
        aliases: tuple[WorkspacePathAlias, ...],
    ) -> tuple[WorkspacePathAlias, ...]:
        normalized: list[WorkspacePathAlias] = []
        for alias in aliases:
            virtual_path = alias.virtual_path.rstrip("/") or "/"
            if not virtual_path.startswith("/"):
                raise ValueError(f"Path alias must be absolute: {alias.virtual_path}")

            host_path = str(Path(alias.host_path).resolve()).rstrip("/") or "/"
            normalized.append(WorkspacePathAlias(virtual_path, host_path))
        return tuple(sorted(normalized, key=lambda item: len(item.virtual_path), reverse=True))

    def virtual_to_host(self, path: str) -> str | None:
        """Return the host mirror for an aliased virtual path, if one matches."""
        if not path.startswith("/"):
            return None
        for alias in self.aliases:
            virtual_path = alias.virtual_path
            if virtual_path == "/":
                suffix = path.removeprefix("/").lstrip("/")
                return str(Path(alias.host_path) / suffix)
            if path == virtual_path:
                return alias.host_path
            if path.startswith(f"{virtual_path}/"):
                suffix = path.removeprefix(virtual_path).lstrip("/")
                return str(Path(alias.host_path) / suffix)
        return None

    def map_virtual_to_host(self, path: str) -> str:
        """Map a virtual path to host if aliased; otherwise return it unchanged."""
        return self.virtual_to_host(path) or path

    def host_to_virtual_path(self, path: str | Path) -> str:
        """Map a host mirror path back to its virtual path if aliased."""
        text = str(path)
        for host_prefix, virtual_prefix in self._host_to_virtual_rewrites():
            mapped = self._replace_path_prefix(text, host_prefix, virtual_prefix)
            if mapped != text:
                return mapped
        return text

    def replace_host_paths(self, text: str) -> str:
        """Replace known host mirror prefixes embedded in text with virtual paths."""
        sanitized = text
        for host_prefix, virtual_prefix in self._host_to_virtual_rewrites():
            sanitized = self._replace_path_prefix(sanitized, host_prefix, virtual_prefix)
        return sanitized

    def sanitize_host_paths(self, value: Any) -> Any:
        """Recursively rewrite host mirror paths inside common observation objects."""
        if isinstance(value, str):
            return self.replace_host_paths(value)
        if isinstance(value, list):
            return [self.sanitize_host_paths(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.sanitize_host_paths(item) for item in value)
        if isinstance(value, dict):
            return {
                key: self.sanitize_host_paths(item)
                for key, item in value.items()
            }

        model_copy = getattr(value, "model_copy", None)
        model_fields = getattr(type(value), "model_fields", None)
        if callable(model_copy) and isinstance(model_fields, dict):
            updates: dict[str, Any] = {}
            for field_name in model_fields:
                if not hasattr(value, field_name):
                    continue
                original = getattr(value, field_name)
                sanitized = self.sanitize_host_paths(original)
                if sanitized != original:
                    updates[field_name] = sanitized
            if updates:
                return model_copy(update=updates)
        return value

    def _host_to_virtual_rewrites(self) -> tuple[tuple[str, str], ...]:
        rewrites: dict[str, str] = {}
        for alias in self.aliases:
            for candidate in (Path(alias.host_path), Path(alias.host_path).resolve()):
                host_path = str(candidate).rstrip("/")
                if host_path:
                    rewrites[host_path] = alias.virtual_path.rstrip("/") or "/"
        return tuple(
            sorted(rewrites.items(), key=lambda rewrite: len(rewrite[0]), reverse=True)
        )

    @staticmethod
    def _replace_path_prefix(
        text: str,
        source_prefix: str,
        target_prefix: str,
    ) -> str:
        pattern = re.compile(rf"{re.escape(source_prefix)}(?=$|/)")
        return pattern.sub(target_prefix, text)
