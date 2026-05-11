"""Scripts-first enforcer for `experiments/<study>/reports/` directories.

For every file, require proof that a committed script produced it from
recorded inputs:

* `.md` files require YAML frontmatter with `generated_by`, `template`,
  `inputs`, `generated_at`, `inputs_sha256`, and `output_sha256`. Each
  listed input sha256 is recomputed and compared. The template entry in
  `inputs_sha256` is required too.
* Other files require a matching entry in the directory's `.generated.json`
  sidecar. Both input sha256s and the stored `output_sha256` are re-verified.
* The sidecar itself (`.generated.json`) is the validator's own metadata
  store and is skipped by the walker — integrity is guaranteed by the
  atomic writer plus this same validator running at commit time.

Exit code 0 on success, 1 on any violation. Offending paths and reasons
are printed to stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from experiments.shared.scripts._paths import (
    get_repo_root,
    resolve_repo_path,
    to_repo_relative,
    to_repo_relative_strict,
)
from experiments.shared.scripts.write_report import GENERATED_JSON


if TYPE_CHECKING:
    from collections.abc import Iterable


logger = logging.getLogger(__name__)


# Required frontmatter keys for .md files.
_REQUIRED_MD_KEYS: tuple[str, ...] = (
    "generated_by",
    "template",
    "inputs",
    "generated_at",
    "inputs_sha256",
    "output_sha256",
)

# Required keys for each sidecar entry.
_REQUIRED_ENTRY_KEYS: tuple[str, ...] = (
    "generated_by",
    "inputs",
    "inputs_sha256",
    "generated_at",
    "output_sha256",
)

_FRONTMATTER_DELIM_LINE = b"---\n"


def _sha256_of(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _discover_report_files() -> list[Path]:
    """Return every file under any `experiments/*/reports/` tree."""
    experiments_dir = get_repo_root() / "experiments"
    results: list[Path] = []
    if not experiments_dir.is_dir():
        return results

    for study_dir in sorted(experiments_dir.iterdir()):
        if not study_dir.is_dir():
            continue
        reports_dir = study_dir / "reports"
        if not reports_dir.is_dir():
            continue
        for entry in sorted(reports_dir.rglob("*")):
            if entry.is_file():
                results.append(entry)
    return results


def _parse_frontmatter(raw: bytes) -> tuple[dict[str, Any], bytes] | None:
    """Return ``(meta, body_bytes)`` for a frontmatter-fenced document.

    Operates on bytes end-to-end so sha256 comparisons match the writer's
    on-disk layout byte-for-byte (no text-mode newline translation). Returns
    ``None`` if the frontmatter is missing or malformed.

    Layout expected (matches `write_md`):
        ``---\\n<yaml>---\\n\\n<body>``
    The single ``\\n`` between the closing fence and the body is the
    separator the writer inserts and the validator strips before hashing.
    """
    if not raw.startswith(_FRONTMATTER_DELIM_LINE):
        return None

    # Locate the closing fence at byte level.
    search_from = len(_FRONTMATTER_DELIM_LINE)
    close_idx = raw.find(b"\n" + _FRONTMATTER_DELIM_LINE, search_from - 1)
    # The fence pattern above finds ``\n---\n``. If not found, try the case
    # where the closing fence sits on the very first line after the opener
    # (empty YAML) — still delimited by ``\n``.
    if close_idx == -1:
        # Also tolerate the file ending exactly with ``---\n`` (no body).
        if raw[search_from:].startswith(_FRONTMATTER_DELIM_LINE):
            close_idx = search_from - 1
        else:
            return None

    yaml_payload = raw[search_from : close_idx + 1]
    body_start = close_idx + 1 + len(_FRONTMATTER_DELIM_LINE)

    try:
        parsed = yaml.safe_load(yaml_payload) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None

    body_bytes = raw[body_start:]
    # Strip a single leading newline that write_md inserts between the
    # closing fence and the body — the writer hashes the body without that
    # separator, so the validator must too.
    if body_bytes.startswith(b"\n"):
        body_bytes = body_bytes[1:]
    return parsed, body_bytes


def _validate_stored_path(
    stored: Any,
    field_name: str,
    context: str,
    errors: list[str],
) -> str | None:
    """Ensure a recorded path is a repo-relative, non-escaping string.

    Returns the validated path on success, or None with an error appended on
    any violation (non-string, absolute, contains `..`, empty, etc.).
    """
    if not isinstance(stored, str):
        errors.append(f"{context}: {field_name} must be a string")
        return None
    try:
        to_repo_relative_strict(stored)
    except ValueError as exc:
        errors.append(f"{context}: {field_name} rejected: {exc}")
        return None
    return stored


def _validate_inputs_hashes(
    inputs_sha256: dict[str, str],
    errors: list[str],
    context: str,
) -> None:
    for rel, expected in inputs_sha256.items():
        # Every key is a stored path — enforce strict shape before touching
        # the filesystem.
        validated = _validate_stored_path(rel, f"inputs_sha256[{rel!r}]", context, errors)
        if validated is None:
            continue
        absolute = resolve_repo_path(validated)
        if not absolute.is_file():
            errors.append(f"{context}: input {validated} listed in inputs_sha256 does not exist")
            continue
        actual = _sha256_of(absolute)
        if actual != expected:
            errors.append(
                f"{context}: sha256 mismatch for input {validated} "
                f"(stored={expected[:12]}... actual={actual[:12]}...)"
            )


def _validate_markdown(path: Path, errors: list[str]) -> None:
    rel = to_repo_relative(path)
    raw = _safe_read_bytes(path, errors, context=rel)
    if raw is None:
        return

    parsed = _parse_frontmatter(raw)
    if parsed is None:
        errors.append(f"{rel}: missing or malformed YAML frontmatter")
        return
    frontmatter, body_bytes = parsed

    if not _check_required_keys(frontmatter, _REQUIRED_MD_KEYS, errors, context=rel):
        return
    if not _check_md_paths(frontmatter, errors, context=rel):
        return
    if not _check_inputs_consistency_md(frontmatter, errors, context=rel):
        return
    _validate_inputs_hashes(frontmatter["inputs_sha256"], errors, context=rel)
    _check_body_sha256(frontmatter, body_bytes, errors, context=rel)


def _safe_read_bytes(path: Path, errors: list[str], *, context: str) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError as exc:
        errors.append(f"{context}: unreadable ({exc})")
        return None


def _check_required_keys(
    frontmatter: dict[str, Any],
    required: tuple[str, ...],
    errors: list[str],
    *,
    context: str,
) -> bool:
    missing = [key for key in required if key not in frontmatter]
    if missing:
        errors.append(f"{context}: frontmatter missing required keys: {', '.join(missing)}")
        return False
    return True


def _check_md_paths(
    frontmatter: dict[str, Any],
    errors: list[str],
    *,
    context: str,
) -> bool:
    """Verify every recorded path (generated_by, template, inputs) is safe."""
    ok = True

    generated_by = _validate_stored_path(
        frontmatter.get("generated_by"), "generated_by", context, errors
    )
    if generated_by is None:
        ok = False
    elif not resolve_repo_path(generated_by).is_file():
        errors.append(f"{context}: generated_by script {generated_by} does not exist")
        ok = False

    template = _validate_stored_path(frontmatter.get("template"), "template", context, errors)
    if template is None:
        ok = False
    elif not resolve_repo_path(template).is_file():
        errors.append(f"{context}: template {template} does not exist")
        ok = False

    declared_inputs = frontmatter.get("inputs") or []
    if not isinstance(declared_inputs, list):
        errors.append(f"{context}: inputs must be a list of strings")
        return False
    for item in declared_inputs:
        validated = _validate_stored_path(item, "inputs entry", context, errors)
        if validated is None:
            ok = False

    return ok


def _check_inputs_consistency_md(
    frontmatter: dict[str, Any],
    errors: list[str],
    *,
    context: str,
) -> bool:
    """For markdown reports, `inputs_sha256` is a superset of `inputs`.

    The template hash lives in `inputs_sha256` too, but the caller-facing
    `inputs` list does NOT include it. So the consistency rule is:
    - every declared input must have a sha256 entry,
    - the template path must appear as an `inputs_sha256` key.
    """
    inputs_sha256 = frontmatter.get("inputs_sha256") or {}
    if not isinstance(inputs_sha256, dict):
        errors.append(f"{context}: inputs_sha256 must be a mapping")
        return False

    declared_inputs = frontmatter.get("inputs") or []
    if not isinstance(declared_inputs, list) or not all(
        isinstance(i, str) for i in declared_inputs
    ):
        errors.append(f"{context}: inputs must be a list of strings")
        return False

    missing_hashes = [i for i in declared_inputs if i not in inputs_sha256]
    if missing_hashes:
        errors.append(f"{context}: inputs_sha256 missing entries for: {', '.join(missing_hashes)}")
        return False

    template = frontmatter.get("template")
    if isinstance(template, str) and template not in inputs_sha256:
        errors.append(f"{context}: inputs_sha256 must include the template path {template}")
        return False

    return True


def _check_body_sha256(
    frontmatter: dict[str, Any],
    body_bytes: bytes,
    errors: list[str],
    *,
    context: str,
) -> None:
    stored = frontmatter.get("output_sha256")
    if not isinstance(stored, str):
        errors.append(f"{context}: output_sha256 must be a hex string")
        return
    actual = hashlib.sha256(body_bytes).hexdigest()
    if actual != stored:
        errors.append(
            f"{context}: sha256 mismatch for markdown body "
            f"(stored={stored[:12]}... actual={actual[:12]}...)"
        )


def _check_entry_generated_by(entry: dict[str, Any], errors: list[str], *, context: str) -> bool:
    generated_by = _validate_stored_path(entry.get("generated_by"), "generated_by", context, errors)
    if generated_by is None:
        return False
    if not resolve_repo_path(generated_by).is_file():
        errors.append(f"{context}: generated_by script {generated_by} does not exist")
        return False
    return True


def _check_entry_inputs(entry: dict[str, Any], errors: list[str], *, context: str) -> bool:
    inputs_sha256 = entry.get("inputs_sha256") or {}
    if not isinstance(inputs_sha256, dict):
        errors.append(f"{context}: inputs_sha256 must be a mapping")
        return False

    declared_inputs = entry.get("inputs") or []
    if not isinstance(declared_inputs, list) or not all(
        isinstance(i, str) for i in declared_inputs
    ):
        errors.append(f"{context}: inputs must be a list of strings")
        return False

    for item in declared_inputs:
        if _validate_stored_path(item, "inputs entry", context, errors) is None:
            return False

    if set(inputs_sha256.keys()) != set(declared_inputs):
        errors.append(f"{context}: inputs list and inputs_sha256 keys must match exactly")
        return False

    return True


def _check_entry_shape(entry: dict[str, Any], errors: list[str], *, context: str) -> bool:
    """Validate sidecar entry structure (keys, types, recorded paths)."""
    missing = [key for key in _REQUIRED_ENTRY_KEYS if key not in entry]
    if missing:
        errors.append(
            f"{context}: `.generated.json` entry missing required keys: {', '.join(missing)}"
        )
        return False

    if not _check_entry_generated_by(entry, errors, context=context):
        return False
    return _check_entry_inputs(entry, errors, context=context)


def _check_output_sha(
    path: Path, entry: dict[str, Any], errors: list[str], *, context: str
) -> None:
    stored = entry.get("output_sha256")
    if not isinstance(stored, str):
        errors.append(f"{context}: output_sha256 must be a hex string")
        return
    actual = _sha256_of(path)
    if actual != stored:
        errors.append(
            f"{context}: sha256 mismatch for output "
            f"(stored={stored[:12]}... actual={actual[:12]}...)"
        )


def _validate_binary(
    path: Path, sidecar_index: dict[str, dict[str, Any]], errors: list[str]
) -> None:
    rel = to_repo_relative(path)
    entry = sidecar_index.get(rel)
    if entry is None:
        errors.append(f"{rel}: no `.generated.json` entry for this file")
        return

    if not _check_entry_shape(entry, errors, context=rel):
        return

    _validate_inputs_hashes(entry["inputs_sha256"], errors, context=rel)
    _check_output_sha(path, entry, errors, context=rel)


def _load_sidecar(reports_dir: Path) -> dict[str, dict[str, Any]]:
    """Load `<reports_dir>/.generated.json` as a mapping of rel-path → entry.

    Audit N-9: raises on parse failure / non-dict instead of silently
    treating a corrupt sidecar as empty (which would flag every binary
    in the directory as "no entry" and mask the actual corruption).
    """
    index = reports_dir / GENERATED_JSON
    if not index.is_file():
        return {}
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"sidecar {index} is not valid JSON; cannot validate the binaries "
            "it covers — repair or remove the file"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"sidecar {index} is not a JSON object; cannot validate the binaries it covers"
        )
    typed: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, dict):
            typed[key] = value
    return typed


def _reports_root_for(path: Path) -> Path | None:
    """Find the `<study>/reports/` ancestor for a file, or None if outside."""
    experiments_dir = get_repo_root() / "experiments"
    for parent in path.parents:
        if parent.name == "reports" and parent.parent.parent == experiments_dir:
            return parent
    return None


def validate_paths(paths: Iterable[Path]) -> list[str]:
    """Return a list of human-readable error messages (empty = valid)."""
    errors: list[str] = []
    sidecars: dict[Path, dict[str, dict[str, Any]]] = {}

    for path in paths:
        if path.is_dir():
            continue
        # Skip ONLY the sidecar itself. Any other dotfile (including the old
        # `.generated.json.lock` layout or anything starting with the
        # basename) is treated as a stray file and must carry proof just
        # like every other file in reports/.
        if path.name == GENERATED_JSON:
            continue

        reports_dir = _reports_root_for(path.resolve())
        if reports_dir is None:
            errors.append(
                f"{to_repo_relative(path)}: not under any `experiments/<study>/reports/` tree"
            )
            continue

        if path.suffix.lower() == ".md":
            _validate_markdown(path, errors)
        else:
            if reports_dir not in sidecars:
                sidecars[reports_dir] = _load_sidecar(reports_dir)
            _validate_binary(path, sidecars[reports_dir], errors)

    return errors


def validate_study(study_id: str) -> list[str]:
    """Study-level invariants that complement the per-file walker.

    Returns a list of error messages (empty = valid). Enforces, in order:

    1. ``experiments/<study>/reports/enrollment.lock.yaml`` exists.
    2. ``experiments/<study>/manifest.yaml`` does NOT carry a ``runs:`` key
       (PR 1: roster moved to the lockfile; a regression here would silently
       reintroduce mixed-lifecycle state into the design file).
    3. Every ``run_id`` listed in the lockfile resolves to a discoverable
       ``run_manifest.json`` somewhere in the pool roots.
    """
    # Local imports keep this module's import surface small for the file
    # walker (the historical entry point) — only the study path needs them.
    from experiments.shared.scripts.collect import (
        ENROLLMENT_LOCK_FILENAME,
        load_enrollment_lock,
    )
    from experiments.shared.scripts.load_runs import iter_run_manifests

    errors: list[str] = []
    study_dir = get_repo_root() / "experiments" / study_id
    manifest_path = study_dir / "manifest.yaml"
    lock_path = study_dir / "reports" / ENROLLMENT_LOCK_FILENAME

    if not lock_path.is_file():
        errors.append(
            f"experiments/{study_id}/reports/{ENROLLMENT_LOCK_FILENAME}: missing; "
            "run `python -m experiments.shared.scripts.collect "
            f"--study {study_id}`"
        )

    if manifest_path.is_file():
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if isinstance(manifest, dict) and "runs" in manifest:
            errors.append(
                f"experiments/{study_id}/manifest.yaml: contains a `runs:` key; "
                "the enrollment roster lives in reports/enrollment.lock.yaml now"
            )
    else:
        errors.append(f"experiments/{study_id}/manifest.yaml: not found")

    if lock_path.is_file():
        try:
            lock = load_enrollment_lock(study_id)
        except (FileNotFoundError, ValueError) as exc:
            errors.append(f"experiments/{study_id}/reports/{ENROLLMENT_LOCK_FILENAME}: {exc}")
            return errors

        enrolled = lock.get("enrollment") or []
        # Audit N-10: build a run_id → manifest-path index so we can
        # compare the full (study_id, cell, task, replicate) tuple against
        # each enrolled row, not just the run_id's presence. Pre-fix, a
        # manifest re-stamped under a different study/cell silently
        # passed validation because only run-id presence was checked.
        pool_manifests: dict[str, Path] = {}
        for path in iter_run_manifests():
            pool_manifests[path.parent.name] = path
        for entry in enrolled:
            if not isinstance(entry, dict):
                continue
            run_id = entry.get("run_id")
            if not isinstance(run_id, str) or run_id not in pool_manifests:
                errors.append(
                    f"experiments/{study_id}/reports/{ENROLLMENT_LOCK_FILENAME}: "
                    f"run_id {run_id!r} has no run_manifest.json in any pool root"
                )
                continue
            manifest_path = pool_manifests[run_id]
            try:
                record = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(
                    f"experiments/{study_id}/reports/{ENROLLMENT_LOCK_FILENAME}: "
                    f"run_id {run_id!r}: run_manifest.json unreadable: {exc}"
                )
                continue
            if not isinstance(record, dict):
                errors.append(
                    f"experiments/{study_id}/reports/{ENROLLMENT_LOCK_FILENAME}: "
                    f"run_id {run_id!r}: run_manifest.json is not a JSON object"
                )
                continue
            # Replicate keys vary across legacy manifests; treat absence as -1
            # so the tuple-compare still surfaces drift when one side has it
            # and the other doesn't.
            manifest_replicate = record.get("replicate")
            if manifest_replicate is None:
                manifest_replicate = record.get("attempt")
            expected = (
                entry.get("study_id", study_id),
                entry.get("cell"),
                entry.get("task"),
                entry.get("replicate"),
            )
            actual = (
                record.get("study_id"),
                record.get("cell"),
                record.get("task"),
                manifest_replicate,
            )
            if expected != actual:
                errors.append(
                    f"experiments/{study_id}/reports/{ENROLLMENT_LOCK_FILENAME}: "
                    f"run_id {run_id!r}: enrollment tuple drift "
                    f"lock={expected} manifest={actual}"
                )

    return errors


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_reports",
        description="Validate scripts-first provenance of experiment report files.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Specific files to validate; defaults to all files under experiments/*/reports/",
    )
    parser.add_argument(
        "--study",
        action="append",
        dest="studies",
        help=(
            "Run study-level invariants for this study id (repeatable). "
            "Checks enrollment.lock.yaml exists, manifest.yaml has no `runs:` "
            "key, and every enrolled run_id resolves in the pool."
        ),
    )
    return parser


def _discover_study_ids() -> list[str]:
    """Return every ``experiments/<id>/`` whose ``manifest.yaml`` exists."""
    experiments_dir = get_repo_root() / "experiments"
    if not experiments_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in experiments_dir.iterdir()
        if entry.is_dir() and (entry / "manifest.yaml").is_file()
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)

    targets = list(args.paths) if args.paths else _discover_report_files()
    file_errors = validate_paths(targets) if targets else []

    # Auto-discover studies when no specific paths are passed and no
    # explicit --study flag is given. Without this the pre-commit hook
    # (which calls `validate_reports` with no args) would never run the
    # PR 1 invariants, so a regression dropping the lockfile or
    # reintroducing `runs:` in manifest.yaml could slip in unnoticed.
    studies: list[str]
    if args.studies:
        studies = list(args.studies)
    elif not args.paths:
        studies = _discover_study_ids()
    else:
        studies = []

    study_errors: list[str] = []
    for study_id in studies:
        study_errors.extend(validate_study(study_id))

    errors = file_errors + study_errors
    if not errors:
        return 0

    for message in errors:
        print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
