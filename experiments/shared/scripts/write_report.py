"""Scripts-first writers: `write_md` (frontmatter) and `write_binary` (sidecar).

Every file under any `experiments/<study_id>/reports/` directory must prove it
was produced by a committed script from recorded inputs. These writers emit
that proof alongside the content, atomically, so hand-writes can't sneak past
the validator (see `validate_reports.py`).

Requires POSIX (``fcntl``). Windows is not supported in v1.

Usage::

    from experiments.shared.scripts.write_report import write_md, write_binary

    write_md(
        path="experiments/2026-04-22-demo/reports/report.md",
        content=rendered_markdown,
        script=__file__,
        template="experiments/2026-04-22-demo/templates/report.md.j2",
        inputs=["experiments/2026-04-22-demo/reports/tables/summary.csv"],
    )

    write_binary(
        path="experiments/2026-04-22-demo/reports/figures/01-success-rate.png",
        content=png_bytes,
        script=__file__,
        inputs=["experiments/2026-04-22-demo/reports/tables/summary.csv"],
    )
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml


if TYPE_CHECKING:
    from collections.abc import Iterable

from experiments.shared.scripts._paths import resolve_repo_path, to_repo_relative


GENERATED_JSON = ".generated.json"
_FRONTMATTER_DELIM = b"---\n"


def _sha256_of(path: Path) -> str:
    """Stream-hash a file so large inputs (e.g., CSVs, PNGs) don't blow RAM."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha256_of_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _inputs_hash_map(inputs: Iterable[str]) -> dict[str, str]:
    """Compute sha256 for every listed input, keyed by repo-relative path."""
    result: dict[str, str] = {}
    for item in inputs:
        rel = to_repo_relative(item)
        absolute = resolve_repo_path(rel)
        if not absolute.is_file():
            raise FileNotFoundError(f"input does not exist: {rel}")
        result[rel] = _sha256_of(absolute)
    return result


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write bytes via tmp + rename in the same directory (atomic on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _build_frontmatter_bytes(meta: dict[str, Any]) -> bytes:
    """Serialize frontmatter metadata as bytes wrapped in `---` fences.

    All sha256 arithmetic operates on bytes, so the frontmatter block is
    kept in bytes end-to-end to avoid any Python text-mode newline
    translation (CRLF<->LF) sneaking into hashes.
    """
    yaml_payload = yaml.safe_dump(
        meta,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
    ).encode("utf-8")
    return _FRONTMATTER_DELIM + yaml_payload + _FRONTMATTER_DELIM


def write_md(
    *,
    path: str | Path,
    content: str,
    script: str | Path,
    template: str | Path,
    inputs: Iterable[str | Path],
) -> Path:
    """Write a markdown report with scripts-first YAML frontmatter.

    The frontmatter carries sha256 entries for all inputs AND the template
    (under ``inputs_sha256``), plus an ``output_sha256`` over the body bytes
    (everything after the closing fence + the single separator newline the
    writer inserts). Post-hoc edits to the narrative are caught by the
    validator just like binary tampering.

    Returns the absolute destination path.
    """
    output_rel = to_repo_relative(path)
    output_abs = resolve_repo_path(output_rel)

    # Enforce reports-root containment BEFORE any on-disk state changes; a
    # misaimed write otherwise leaves a stray file under, say, `runs/`.
    _reports_root_for_output(output_abs)

    # Materialize inputs once so a one-shot iterator isn't exhausted by the
    # first comprehension and silently dropped from the sha256 map.
    inputs_materialized: list[str] = [str(i) for i in inputs]
    inputs_rel = [to_repo_relative(i) for i in inputs_materialized]

    # Ensure the body starts fresh and newline-separated from the frontmatter.
    body = content if content.endswith("\n") else content + "\n"
    body_bytes = body.encode("utf-8")

    template_rel = to_repo_relative(template)
    inputs_sha = _inputs_hash_map(inputs_materialized)
    # Store template hash alongside input hashes so a swap of the Jinja
    # source is caught by the validator. The `inputs` list stays caller-
    # facing (no template), but `inputs_sha256` is a superset: it MUST
    # include the template entry (spec §7.3 — every contributing file is
    # traceable).
    template_abs = resolve_repo_path(template_rel)
    if not template_abs.is_file():
        raise FileNotFoundError(f"template does not exist: {template_rel}")
    inputs_sha[template_rel] = _sha256_of(template_abs)

    meta = {
        "generated_by": to_repo_relative(script),
        "template": template_rel,
        "inputs": inputs_rel,
        "generated_at": _utc_now_iso(),
        "inputs_sha256": inputs_sha,
        "output_sha256": _sha256_of_bytes(body_bytes),
    }
    frontmatter_bytes = _build_frontmatter_bytes(meta)

    # Layout: `---\n<yaml>---\n\n<body>` — the extra `\n` between the closing
    # fence and the body is the separator the validator strips before hashing.
    payload = frontmatter_bytes + b"\n" + body_bytes

    _atomic_write_bytes(output_abs, payload)
    return output_abs


def write_binary(
    *,
    path: str | Path,
    content: bytes,
    script: str | Path,
    inputs: Iterable[str | Path],
) -> Path:
    """Write a binary report asset and upsert its `.generated.json` entry.

    The entry records the script, inputs, inputs_sha256, generated_at, and the
    sha256 of the output bytes (`output_sha256`). The sidecar lives at the
    ROOT of the enclosing `<study>/reports/` directory so a single
    `.generated.json` covers all binary artifacts in the study (spec §7.1).
    Returns the absolute output path.
    """
    output_rel = to_repo_relative(path)
    output_abs = resolve_repo_path(output_rel)

    # Validate containment BEFORE any on-disk state changes; otherwise a
    # misaimed write leaves a stray file outside the reports tree.
    sidecar_dir = _reports_root_for_output(output_abs)

    # Materialize inputs once (same reasoning as write_md above).
    inputs_materialized: list[str] = [str(i) for i in inputs]
    inputs_rel = [to_repo_relative(i) for i in inputs_materialized]
    inputs_sha = _inputs_hash_map(inputs_materialized)

    entry = {
        "generated_by": to_repo_relative(script),
        "inputs": inputs_rel,
        "inputs_sha256": inputs_sha,
        "generated_at": _utc_now_iso(),
        "output_sha256": _sha256_of_bytes(content),
    }

    _atomic_write_bytes(output_abs, content)
    _upsert_generated_json(sidecar_dir / GENERATED_JSON, output_rel, entry)
    return output_abs


def _reports_root_for_output(output_abs: Path) -> Path:
    """Find the enclosing `<study>/reports/` dir for an output file path."""
    for parent in output_abs.parents:
        if parent.name == "reports" and parent.parent.parent.name == "experiments":
            return parent
    raise ValueError(
        f"output path {output_abs} is not under any `experiments/<study>/reports/` tree"
    )


def _lock_path_for(index_path: Path) -> Path:
    """Return a stable lock file path in the system temp dir.

    Keeping the lock OUT of the reports directory means the validator's
    directory walker never has to special-case a transient lock file, and the
    lock file isn't a candidate for git tracking. The name is derived from
    the sha256 of the resolved index path so two writers targeting the same
    sidecar agree on the lock file while writers targeting different
    sidecars never collide.
    """
    digest = hashlib.sha256(str(index_path.resolve()).encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / f"arise-sec-lion.generated-json.{digest}.lock"


def _upsert_generated_json(index_path: Path, key: str, entry: dict[str, Any]) -> None:
    """Merge `entry` into the sidecar under `key`, taking an exclusive lock.

    Serialization on the same file is guaranteed by `fcntl.LOCK_EX` on a
    stable lock file in the system temp dir (never in `reports/`). The
    on-disk write of the sidecar itself uses tmp + rename so we never leave
    a partial file.
    """
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path_for(index_path)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existing = _read_generated_json(index_path)
        existing[key] = entry
        payload = json.dumps(existing, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        _atomic_write_bytes(index_path, payload)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _read_generated_json(index_path: Path) -> dict[str, Any]:
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data
