"""Byte-level hash of a run's invocation for reproducibility tracking.

The hash combines effective settings (secrets redacted), the domain-context
file bytes (if any), and the task string. Each field is framed with a typed
tag and an 8-byte big-endian length prefix so arbitrary bytes (including
NULs) cannot cause cross-field collisions.
"""

from __future__ import annotations

import hashlib
import json
import struct
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings


# Settings fields that carry secrets and must NEVER enter the hash input.
# Extend this mapping as new secret fields are added to Settings.
_SECRET_EXCLUSIONS: dict[str, dict[str, bool]] = {
    "database": {"password": True},
}


def _redacted_settings_blob(settings: Settings) -> bytes:
    redacted = settings.model_dump(mode="json", exclude=_SECRET_EXCLUSIONS)
    return json.dumps(redacted, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _update_framed(hasher: hashlib._Hash, tag: bytes, payload: bytes) -> None:
    """Feed (tag, uint64_be length, payload) into the hasher.

    Length-prefix framing is injective for arbitrary byte payloads, including
    ones containing NUL bytes, so cross-field ambiguity is impossible.
    """
    hasher.update(tag)
    hasher.update(struct.pack(">Q", len(payload)))
    hasher.update(payload)


def compute_invocation_sha256(
    *,
    settings: Settings,
    task: str,
    domain_context_path: Path | None,
) -> str:
    """Return the sha256 capturing the full reality of a run's invocation.

    Inputs hashed, in order: the effective Settings (minus secrets), the raw
    bytes of the domain-context file (empty if none), and the task string.
    Each field is framed with a typed tag plus an 8-byte big-endian length,
    making the framing injective even when payloads contain arbitrary bytes.
    """
    settings_blob = _redacted_settings_blob(settings)
    ctx_blob = domain_context_path.read_bytes() if domain_context_path else b""
    task_blob = task.encode("utf-8")

    hasher = hashlib.sha256()
    _update_framed(hasher, b"settings:", settings_blob)
    _update_framed(hasher, b"ctx:", ctx_blob)
    _update_framed(hasher, b"task:", task_blob)
    return hasher.hexdigest()
