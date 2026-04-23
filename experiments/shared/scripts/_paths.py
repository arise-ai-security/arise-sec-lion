"""Shared path helpers: repo-root resolution and repo-root-relative normalization.

Every stored path in reports/ headers and `.generated.json` sidecars is
repo-root-relative (see spec §7.1). These helpers keep that invariant in one
place so writers and validators can't disagree.

Tests override the repo root via ``monkeypatch.setattr`` on this module.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


REPO_ROOT: Path = Path(__file__).resolve().parents[3]


def get_repo_root() -> Path:
    """Return the currently-configured repo root (patchable in tests)."""
    return REPO_ROOT


def to_repo_relative(path: str | Path) -> str:
    """Normalize a path to repo-root-relative POSIX form.

    Accepts both absolute paths (which must sit inside the repo) and
    already-relative strings (which are joined against the repo root only for
    the escape check). Raises ValueError if the resulting path escapes the
    repo root.
    """
    root = get_repo_root()
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path {path!r} is outside the repository root {root}") from exc
    return rel.as_posix()


def resolve_repo_path(path: str | Path) -> Path:
    """Return the absolute filesystem path for a repo-root-relative input."""
    root = get_repo_root()
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return (root / candidate).resolve()


def to_repo_relative_strict(candidate: str | Path) -> PurePosixPath:
    """Validate a stored-relative path without touching the filesystem.

    Unlike `to_repo_relative`, which resolves and normalizes, this helper
    checks an already-recorded string for shape violations only — it never
    joins against the filesystem. Rejecting:

    * absolute paths (a sidecar entry claiming `/etc/passwd` must not slip
      past validation just because the hash happens to match),
    * any segment equal to ``..`` (even if the overall path wouldn't escape
      the repo after resolution — the strictest form is simplest to audit),
    * empty strings or paths resolving to the repo root itself.

    Returns a `PurePosixPath` for the caller to use when resolving against a
    known-safe root. Raises ValueError on any violation.
    """
    if candidate is None or candidate == "":
        raise ValueError("recorded path is empty")

    raw = str(candidate)
    pure = PurePosixPath(raw)

    if pure.is_absolute() or raw.startswith(("/", "\\")):
        raise ValueError(f"recorded path must be relative to the repo root: {raw!r}")

    # PurePosixPath normalizes slashes but preserves '..' parts, so a literal
    # token check catches escape attempts even if subsequent segments would
    # re-enter the tree (e.g. 'a/../a/../../etc/passwd').
    if any(part == ".." for part in pure.parts):
        raise ValueError(f"recorded path must not contain '..' segments: {raw!r}")

    if not pure.parts or pure == PurePosixPath("."):
        raise ValueError(f"recorded path must not be empty or refer to repo root: {raw!r}")

    return pure
