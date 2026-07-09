"""Anti-leak sealing and runtime-script templating for SEC-bench containers.

Owns the Arise-owned, non-golden runtime scripts (``repro.sh``, ``patch.sh``,
the delegating ``secb`` wrapper) and the invariants that keep a root worker
shell from tampering with them or reading golden fix/repro artifacts:

- the sealed dir is a sibling of the run workspace with no bind mount;
- the ``secb`` wrapper strips ``REPLAY_ENABLED`` so ``compile`` runs the
  agent-editable ``$SRC/build.sh``, never a baked ``replay_build.sh``;
- ``/testcase/patch.sh`` is seeded ``0555`` and double read-only-overlaid;
- ``_REPRO_SKELETON`` exits ``2`` until the Exploiter replaces it;
- ``_FORBIDDEN_TESTCASE_ARTIFACTS`` are deleted from the host mirror.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from core.ports.domain_plugin_port import SealedRuntimeArtifact, SealedRuntimeSurface


_FORBIDDEN_TESTCASE_ARTIFACTS = frozenset(
    {
        "model_patch.diff",
        "gold.patch",
        "gold_patch.diff",
        "gold_patch.patch",
        "candidate_fix.diff",
        "candidate_fix.patch",
        "candidate_fixes.diff",
        "candidate_fixes.patch",
    }
)

_SECB_WRAPPER = """#!/bin/bash
set -euo pipefail

command="${1:-}"
if [ "$#" -gt 0 ]; then
  shift
fi

case "$command" in
  build)
    # Strip REPLAY_ENABLED so compile always runs $SRC/build.sh (the agent-editable
    # recipe), never a baked golden $SRC/replay_build.sh.
    exec env -u REPLAY_ENABLED /usr/local/bin/compile "$@"
    ;;
  repro)
    exec /testcase/repro.sh "$@"
    ;;
  patch)
    exec /testcase/patch.sh "$@"
    ;;
  *)
    echo "usage: secb {build|repro|patch} [args...]" >&2
    exit 2
    ;;
esac
"""

_REPRO_SKELETON = """#!/bin/bash
set -euo pipefail
echo "Arise seeded an empty /testcase/repro.sh; Exploiter must replace it." >&2
exit 2
"""

_PATCH_SCRIPT = """#!/bin/bash
set -euo pipefail

apply_if_needed() {
  local patch_file="$1"
  if [ ! -s "$patch_file" ]; then
    return 0
  fi
  if git apply --check "$patch_file"; then
    git apply "$patch_file"
    return 0
  fi
  if git apply --reverse --check "$patch_file" >/dev/null 2>&1; then
    echo "$patch_file already applied" >&2
    return 0
  fi
  git apply --check "$patch_file"
}

apply_if_needed /testcase/repo_changes.diff

if [ ! -s /testcase/model_patch.diff ]; then
  echo "/testcase/model_patch.diff is required and must be non-empty" >&2
  exit 1
fi

apply_if_needed /testcase/model_patch.diff
"""


def sealed_runtime_surface() -> SealedRuntimeSurface:
    """Describe the agent-visible runtime surface Arise overwrites (anti-leak).

    The secb wrapper is installed per worker container in ``start_session``,
    but its content/path are fixed constants, so it is described here at
    prep time alongside the repro/patch scripts seeded into ``/testcase``.
    """

    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    return SealedRuntimeSurface(
        surface="secbench",
        artifacts=(
            SealedRuntimeArtifact(
                container_path="/testcase/repro.sh",
                kind="repro_skeleton",
                content_sha256=_sha(_REPRO_SKELETON),
            ),
            SealedRuntimeArtifact(
                container_path="/testcase/patch.sh",
                kind="patch_script",
                content_sha256=_sha(_PATCH_SCRIPT),
            ),
            SealedRuntimeArtifact(
                container_path="/usr/local/bin/secb",
                kind="secb_wrapper",
                content_sha256=_sha(_SECB_WRAPPER),
            ),
        ),
    )


def seed_runtime_scripts(testcase_dir: Path) -> None:
    """Overwrite agent-visible runtime scripts with Arise-owned non-golden versions."""
    replace_regular_file(testcase_dir / "repro.sh", _REPRO_SKELETON, 0o755)
    replace_regular_file(testcase_dir / "patch.sh", _PATCH_SCRIPT, 0o555)


def replace_regular_file(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        tmp_path.chmod(mode)
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def validated_patch_script(testcase_dir: Path) -> Path:
    patch_script = testcase_dir / "patch.sh"
    if patch_script.is_symlink() or not patch_script.is_file():
        raise RuntimeError(f"Refusing to mount non-regular patch helper: {patch_script}")
    testcase_root = testcase_dir.resolve()
    patch_resolved = patch_script.resolve()
    if testcase_root not in patch_resolved.parents:
        raise RuntimeError(f"Refusing to mount patch helper outside testcase dir: {patch_resolved}")
    return patch_script


def sealed_dir(host_root: Path) -> Path:
    """Per-run directory for sealed runtime files the worker must not tamper.

    A sibling of the run workspace, so it sits outside every container bind
    mount (``/src``, ``/testcase``, ``/work``, ``/arise-run``) and has no
    worker path alias. A root worker shell therefore has no filesystem route
    to the host-executed ``secb-exec`` helper or the ``secb`` wrapper source
    — unlike anything under the run workspace, which is all agent-reachable.
    """
    return host_root.parent / f"{host_root.name}.sealed"


def sealed_secb_wrapper(host_root: Path) -> Path:
    """Write the delegating ``secb`` wrapper to the sealed dir; return its path.

    Bound read-only over ``/usr/local/bin/secb`` so a root worker cannot
    rewrite ``secb`` to fake build/repro/patch. The source lives outside
    every bind mount, so there is no writable in-container alias to it.
    """
    secb = sealed_dir(host_root) / "secb"
    replace_regular_file(secb, _SECB_WRAPPER, 0o555)
    return secb


def remove_forbidden_testcase_artifacts(testcase_dir: Path) -> None:
    for artifact_name in _FORBIDDEN_TESTCASE_ARTIFACTS:
        artifact = testcase_dir / artifact_name
        if not artifact.exists() and not artifact.is_symlink():
            continue
        if artifact.is_dir() and not artifact.is_symlink():
            shutil.rmtree(artifact)
        else:
            artifact.unlink()
