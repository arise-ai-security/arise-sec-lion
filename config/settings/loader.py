"""YAML hierarchy loading and environment-variable injection."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


# The config/ directory (one level above this package).
CONFIG_DIR = Path(__file__).resolve().parents[1]


def get_environment() -> str:
    """Get current environment from ARISE_ENV (default: development)."""
    return os.getenv("ARISE_ENV", "development")


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge override into base (override wins)."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml_hierarchy(env: str | None = None) -> dict[str, Any]:
    """Load and merge YAML configs: base <- phase-specific."""
    if env is None:
        env = get_environment()

    base_path = CONFIG_DIR / "config.yaml"
    config: dict[str, Any] = {}
    if base_path.exists():
        with base_path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

    phase_path = CONFIG_DIR / f"config.{env}.yaml"
    if phase_path.exists():
        with phase_path.open(encoding="utf-8") as f:
            phase_config = yaml.safe_load(f) or {}
            config = _merge_dicts(config, phase_config)

    return config


def _inject_env_database(config: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(config)

    db_config = dict(merged.get("database", {}))
    postgres_password = os.getenv("POSTGRES_PASSWORD")
    if postgres_password:
        db_config["password"] = postgres_password
    else:
        db_config.pop("password", None)
    if os.getenv("POSTGRES_HOST"):
        db_config["host"] = os.getenv("POSTGRES_HOST")
    postgres_port = os.getenv("POSTGRES_PORT")
    if postgres_port:
        db_config["port"] = int(postgres_port)
    if os.getenv("POSTGRES_USER"):
        db_config["user"] = os.getenv("POSTGRES_USER")
    if os.getenv("POSTGRES_DB"):
        db_config["name"] = os.getenv("POSTGRES_DB")
    merged["database"] = db_config
    return merged
