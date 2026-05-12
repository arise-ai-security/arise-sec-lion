"""Atomic file write helpers shared across infrastructure adapters.

Each public helper writes a file via the standard POSIX rename dance:
``mkstemp`` in the same directory as the destination, write the payload,
``fsync`` the file descriptor (when ``fsync=True``), then ``os.replace`` the
temp file over the destination. Readers never see a partially written file —
they see either the previous content or the new content, never a torn-write
in between. The parent directory is created (``parents=True``) when missing.

The JSON helper prefers ``orjson`` (already a repo dependency) for speed and
falls back to ``json.dumps`` if ``orjson`` is somehow unavailable at import
time, matching the design described in the concurrency-correctness spec.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


try:
    import orjson

    _HAS_ORJSON = True
except ImportError:  # pragma: no cover - orjson is a declared dependency
    _HAS_ORJSON = False


def write_text(path: Path, content: str, *, fsync: bool = True) -> None:
    """Atomically write ``content`` (UTF-8) to ``path``.

    Args:
        path: Destination file path. Parent directories are created.
        content: Text payload; encoded as UTF-8 before writing.
        fsync: When True (default), ``os.fsync`` the temp file before rename so
            the bytes are flushed to disk. Disable only in tests that do not
            care about durability.
    """
    write_bytes(path, content.encode("utf-8"), fsync=fsync)


def write_bytes(path: Path, content: bytes, *, fsync: bool = True) -> None:
    """Atomically write raw ``content`` bytes to ``path``.

    Args:
        path: Destination file path. Parent directories are created.
        content: Binary payload.
        fsync: When True (default), ``os.fsync`` the temp file before rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def write_json(path: Path, obj: object, *, fsync: bool = True) -> None:
    """Atomically write ``obj`` to ``path`` as indented JSON.

    Uses ``orjson`` when available; falls back to ``json.dumps`` otherwise.
    Output always ends with a trailing newline to match POSIX text-file
    convention (and the existing run-persistence call site).

    Args:
        path: Destination file path.
        obj: JSON-serializable object.
        fsync: When True (default), ``os.fsync`` the temp file before rename.
    """
    if _HAS_ORJSON:
        serialized = orjson.dumps(obj, option=orjson.OPT_INDENT_2) + b"\n"
    else:  # pragma: no cover - orjson is a declared dependency
        serialized = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_bytes(path, serialized, fsync=fsync)
