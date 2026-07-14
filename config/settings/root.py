"""Root settings models: secrets from env, everything else from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .agents import BossConfig, FormatRepairerConfig, ManagerConfig
from .cors import CorsConfig
from .database import DatabaseConfig
from .loader import _inject_env_database, _load_yaml_hierarchy
from .orchestration import OrchestrationConfig
from .output import OutputConfig
from .worker import (
    _WORKER_TOOL_PARAM_TYPES,
    ApiWorkerConfig,
    WorkerConfig,
    _populate_default_tool_params,
)


class ApiSettings(BaseSettings):
    """Query API config that intentionally does not require provider model settings."""

    model_config = SettingsConfigDict(extra="ignore")

    database: DatabaseConfig
    worker: ApiWorkerConfig
    orchestration: OrchestrationConfig
    domain_plugins: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cors: CorsConfig = Field(default_factory=CorsConfig)

    @classmethod
    def _build_from_config(cls, config: dict[str, Any]) -> ApiSettings:
        return cls.model_validate(_inject_env_database(config))

    @classmethod
    def load(cls, env: str | None = None) -> ApiSettings:
        config = _load_yaml_hierarchy(env)
        return cls._build_from_config(config)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> ApiSettings:
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        from config._paths import get_repo_root
        from config.overlay import resolve_overlay

        config = resolve_overlay(config_file, repo_root=get_repo_root())
        return cls._build_from_config(config)


class Settings(BaseSettings):
    """Root config: secrets from env, everything else from YAML."""

    model_config = SettingsConfigDict(extra="forbid")

    database: DatabaseConfig
    boss: BossConfig
    manager: ManagerConfig
    worker: WorkerConfig
    format_repairer: FormatRepairerConfig
    orchestration: OrchestrationConfig
    output: OutputConfig
    domain_plugins: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cors: CorsConfig = Field(default_factory=CorsConfig)

    @model_validator(mode="after")
    def _validate_worker_tool_params(self) -> Settings:
        """Tagged-union check: ``worker.tool_params.<tool>`` must be populated
        when ``worker.tool`` selects that tool, and no other slot may carry a
        payload (catches operator typos like ``tool: openhands`` paired with
        a stray ``claude_code:`` block).
        """
        active = self.worker.tool
        slot = getattr(self.worker.tool_params, active)
        if slot is None:
            raise ValueError(
                f"worker.tool_params.{active} must be populated when worker.tool={active!r}"
            )
        populated_others = [
            name
            for name in _WORKER_TOOL_PARAM_TYPES
            if name != active and getattr(self.worker.tool_params, name) is not None
        ]
        if populated_others:
            raise ValueError(
                f"worker.tool_params has populated slots for inactive tools: "
                f"{populated_others} (active tool is {active!r}). "
                f"Remove the unused slots or change worker.tool."
            )
        return self

    @classmethod
    def _build_from_config(cls, config: dict[str, Any]) -> Settings:
        """Build Settings from config dict, injecting env vars."""
        merged = _inject_env_database(config)

        merged["worker"] = _populate_default_tool_params(dict(merged.get("worker", {})))

        return cls.model_validate(merged)

    @classmethod
    def load(cls, env: str | None = None) -> Settings:
        """Load settings: YAML config + env secrets."""
        config = _load_yaml_hierarchy(env)
        return cls._build_from_config(config)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> Settings:
        """Load from a specific YAML file (for tests) or an overlay file.

        Overlay support: if the YAML declares ``extends:`` + ``overrides:``,
        they are resolved recursively against the base before validation.
        """
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        from config._paths import get_repo_root
        from config.overlay import resolve_overlay

        config = resolve_overlay(config_file, repo_root=get_repo_root())
        return cls._build_from_config(config)
