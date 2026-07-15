"""Strict evaluation-side mirror of the solver-authored PatchPlan schema."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError


class PatchOperation(BaseModel):
    """One exact source transformation anchored to unique text."""

    model_config = {"extra": "forbid", "frozen": True}

    kind: Literal["insert_before", "insert_after", "replace", "delete"]
    anchor: str = Field(min_length=1)
    expected_occurrences: int = Field(default=1, ge=1)
    replacement: str = ""


class PatchPlan(BaseModel):
    """Frozen Root-Cause-Analyst plan consumed literally by Patch-Applier."""

    model_config = {"extra": "forbid", "frozen": True}

    schema_version: Literal["1"] = "1"
    evidence_references: tuple[str, ...] = Field(min_length=1)
    target_file: str
    target_symbol: str
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: tuple[PatchOperation, ...] = Field(min_length=1)
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    forbidden_paths: tuple[str, ...] = Field(min_length=1)
    required_postconditions: tuple[str, ...] = Field(min_length=1)
    validation_commands: tuple[str, ...] = Field(min_length=1)


def normalized_patch_plan(path: Path) -> dict[str, Any] | None:
    """Return canonical judge evidence only for a regular, schema-valid plan file."""
    try:
        payload = _read_regular_bytes(path)
        plan = PatchPlan.model_validate_json(payload)
    except (OSError, ValidationError):
        return None
    return plan.model_dump(mode="json", exclude_none=False, by_alias=True)


def _read_regular_bytes(path: Path) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError(f"refusing to read non-regular file: {path}")
        return stream.read()
