"""Tests for `experiments.shared.scripts.validate_manifest`.

The validator reads `experiments/shared/groups.yaml` from the repo root,
so each test stages a tmp `experiments/` tree and points the validator at
it via `monkeypatch.setattr(config._paths, "REPO_ROOT", tmp_path)`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from config import _paths as config_paths
from experiments.shared.scripts import validate_manifest as vm


if TYPE_CHECKING:
    from pathlib import Path


# -----------------------------
# Fixtures
# -----------------------------


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reroute `config._paths.REPO_ROOT` to an isolated tmp dir.

    The validator calls `from config._paths import get_repo_root`, so the
    only path patch that changes its view is on `config._paths`. Patching
    `experiments.shared.scripts._paths` (the harness's repo-root) would not
    redirect the validator.
    """
    monkeypatch.setattr(config_paths, "REPO_ROOT", tmp_path)
    return tmp_path


def _write_groups(repo_root: Path, mapping: dict[str, str]) -> None:
    path = repo_root / "experiments" / "shared" / "groups.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")


def _write_study(
    repo_root: Path,
    *,
    study_id: str,
    manifest: dict,
    config_files: list[str] | None = None,
) -> Path:
    study_dir = repo_root / "experiments" / study_id
    study_dir.mkdir(parents=True, exist_ok=True)
    (study_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    for cfg in config_files or []:
        cfg_path = study_dir / cfg
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("# placeholder cell config\n")
    return study_dir


def _well_formed_manifest(study_id: str) -> dict:
    return {
        "study_id": study_id,
        "schema_version": 2,
        "cells": {
            "A1": {"group": "A", "runner": "aris", "config": "configs/A1.yaml"},
            "B1": {"group": "B", "runner": "aris", "config": "configs/B1.yaml"},
        },
    }


# -----------------------------
# Happy path
# -----------------------------


def test_validates_well_formed_manifest(repo_root: Path) -> None:
    # Given: a study that satisfies every invariant.
    _write_groups(repo_root, {"A": "Claude Code CLI", "B": "Our System"})
    study_id = "2030-01-01-ok"
    _write_study(
        repo_root,
        study_id=study_id,
        manifest=_well_formed_manifest(study_id),
        config_files=["configs/A1.yaml", "configs/B1.yaml"],
    )
    manifest = _well_formed_manifest(study_id)

    # When/Then: validate returns silently.
    vm.validate_manifest(manifest, repo_root=repo_root)


# -----------------------------
# Required-field errors
# -----------------------------


def test_missing_group_field_raises(repo_root: Path) -> None:
    # Given
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    study_id = "missing-group"
    _write_study(
        repo_root,
        study_id=study_id,
        manifest={"study_id": study_id, "cells": {"A1": {"runner": "aris", "config": "x.yaml"}}},
        config_files=["x.yaml"],
    )
    manifest = {
        "study_id": study_id,
        "cells": {"A1": {"runner": "aris", "config": "x.yaml"}},
    }

    # When/Then
    with pytest.raises(ValueError, match=r"cells\.A1: missing 'group'"):
        vm.validate_manifest(manifest, repo_root=repo_root)


def test_missing_runner_field_raises(repo_root: Path) -> None:
    # Given
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    study_id = "missing-runner"
    _write_study(
        repo_root,
        study_id=study_id,
        manifest={"study_id": study_id, "cells": {"A1": {"group": "A", "config": "x.yaml"}}},
        config_files=["x.yaml"],
    )
    manifest = {
        "study_id": study_id,
        "cells": {"A1": {"group": "A", "config": "x.yaml"}},
    }

    # When/Then
    with pytest.raises(ValueError, match=r"cells\.A1: missing 'runner'"):
        vm.validate_manifest(manifest, repo_root=repo_root)


def test_missing_config_field_raises(repo_root: Path) -> None:
    # Given
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    study_id = "missing-config"
    _write_study(
        repo_root,
        study_id=study_id,
        manifest={"study_id": study_id, "cells": {"A1": {"group": "A", "runner": "aris"}}},
    )
    manifest = {
        "study_id": study_id,
        "cells": {"A1": {"group": "A", "runner": "aris"}},
    }

    # When/Then
    with pytest.raises(ValueError, match=r"cells\.A1: missing 'config'"):
        vm.validate_manifest(manifest, repo_root=repo_root)


# -----------------------------
# Group / runner registry errors
# -----------------------------


def test_unknown_group_raises(repo_root: Path) -> None:
    # Given: groups.yaml only declares A; manifest references Z.
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    study_id = "unknown-group"
    _write_study(
        repo_root,
        study_id=study_id,
        manifest={
            "study_id": study_id,
            "cells": {"Z9": {"group": "Z", "runner": "aris", "config": "x.yaml"}},
        },
        config_files=["x.yaml"],
    )
    manifest = {
        "study_id": study_id,
        "cells": {"Z9": {"group": "Z", "runner": "aris", "config": "x.yaml"}},
    }

    # When/Then
    with pytest.raises(ValueError, match=r"cells\.Z9\.group = 'Z' not in groups\.yaml"):
        vm.validate_manifest(manifest, repo_root=repo_root)


def test_validate_manifest_unknown_group_does_not_emit_prefix_error(repo_root: Path) -> None:
    """Given cells.X1.group = 'Z' where Z is not in groups.yaml, only the
    'not in groups.yaml' error is emitted; no spurious prefix-mismatch
    error mentioning the unknown letter."""

    # Given: groups.yaml only declares A; manifest's X1 cell references Z.
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    manifest = {
        "study_id": "unknown-group-no-prefix",
        "cells": {"X1": {"group": "Z", "runner": "aris", "config": "x.yaml"}},
    }

    # When: validation runs.
    with pytest.raises(ValueError, match=r"cells\.X1\.group = 'Z' not in groups\.yaml") as exc_info:
        vm.validate_manifest(manifest, repo_root=repo_root)

    # Then: no prefix-mismatch error mentioning the unknown letter is emitted.
    assert "must start with group letter" not in str(exc_info.value)


def test_unknown_runner_raises(repo_root: Path) -> None:
    # Given
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    study_id = "unknown-runner"
    _write_study(
        repo_root,
        study_id=study_id,
        manifest={
            "study_id": study_id,
            "cells": {"A1": {"group": "A", "runner": "ghost", "config": "x.yaml"}},
        },
        config_files=["x.yaml"],
    )
    manifest = {
        "study_id": study_id,
        "cells": {"A1": {"group": "A", "runner": "ghost", "config": "x.yaml"}},
    }

    # When/Then
    with pytest.raises(ValueError, match=r"cells\.A1\.runner = 'ghost' not registered"):
        vm.validate_manifest(manifest, repo_root=repo_root)


# -----------------------------
# Cell-name / group-letter mismatch
# -----------------------------


def test_cell_name_must_start_with_group_letter(repo_root: Path) -> None:
    # Given: B1 declared as group A.
    _write_groups(repo_root, {"A": "Claude Code CLI", "B": "Our System"})
    study_id = "cell-prefix"
    _write_study(
        repo_root,
        study_id=study_id,
        manifest={
            "study_id": study_id,
            "cells": {"B1": {"group": "A", "runner": "aris", "config": "x.yaml"}},
        },
        config_files=["x.yaml"],
    )
    manifest = {
        "study_id": study_id,
        "cells": {"B1": {"group": "A", "runner": "aris", "config": "x.yaml"}},
    }

    # When/Then
    with pytest.raises(ValueError, match=r"cells\.B1: name must start with group letter 'A'"):
        vm.validate_manifest(manifest, repo_root=repo_root)


# -----------------------------
# Filesystem-level config check
# -----------------------------


def test_missing_config_file_raises(repo_root: Path) -> None:
    # Given: manifest references a config file that does not exist on disk.
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    study_id = "missing-cfg-file"
    _write_study(
        repo_root,
        study_id=study_id,
        manifest={
            "study_id": study_id,
            "cells": {"A1": {"group": "A", "runner": "aris", "config": "configs/ghost.yaml"}},
        },
        # No config_files written.
    )
    manifest = {
        "study_id": study_id,
        "cells": {"A1": {"group": "A", "runner": "aris", "config": "configs/ghost.yaml"}},
    }

    # When/Then
    with pytest.raises(ValueError, match=r"cells\.A1\.config not found:"):
        vm.validate_manifest(manifest, repo_root=repo_root)


def test_validate_manifest_non_string_config_raises_helpfully(repo_root: Path) -> None:
    """Given a cell with config: 42 (non-string), validator emits an
    actionable type error instead of crashing with TypeError."""

    # Given: a cell whose `config` is an int rather than a string path.
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    manifest = {
        "study_id": "non-string-config",
        "cells": {"A1": {"group": "A", "runner": "aris", "config": 42}},
    }

    # When/Then: validator emits a typed error, not a TypeError crash.
    with pytest.raises(ValueError, match=r"cells\.A1\.config must be a string path \(got int\)"):
        vm.validate_manifest(manifest, repo_root=repo_root)


# -----------------------------
# groups.yaml shape errors
# -----------------------------


def test_invalid_group_letter_in_groups_yaml_raises(repo_root: Path) -> None:
    # Given: groups.yaml uses two-letter and lowercase keys.
    _write_groups(repo_root, {"AA": "double-letter", "alpha": "lowercase"})
    manifest = {"study_id": "x", "cells": {"A1": {"group": "A", "runner": "aris", "config": "y"}}}

    # When/Then
    with pytest.raises(ValueError, match=r"group keys must be single uppercase letters A-Z"):
        vm.validate_manifest(manifest, repo_root=repo_root)


def test_groups_yaml_missing_raises(repo_root: Path) -> None:
    # Given: experiments/ exists but groups.yaml has not been written.
    (repo_root / "experiments" / "shared").mkdir(parents=True, exist_ok=True)
    manifest = {"study_id": "x", "cells": {"A1": {"group": "A", "runner": "aris", "config": "y"}}}

    # When/Then
    with pytest.raises(FileNotFoundError, match=r"groups registry missing"):
        vm.validate_manifest(manifest, repo_root=repo_root)


# -----------------------------
# Top-level manifest field errors
# -----------------------------


def test_empty_cells_block_raises(repo_root: Path) -> None:
    # Given
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    manifest = {"study_id": "study-x", "cells": {}}

    # When/Then
    with pytest.raises(ValueError, match=r"manifest\.cells is empty"):
        vm.validate_manifest(manifest, repo_root=repo_root)


def test_validate_manifest_non_dict_cells_block_raises_helpfully(repo_root: Path) -> None:
    """Given cells: [A1, B1] (list, not mapping), validator emits a shape
    error instead of crashing with AttributeError."""

    # Given: `cells` is a list rather than a mapping.
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    manifest = {"study_id": "list-cells", "cells": ["A1", "B1"]}

    # When/Then: validator surfaces a shape error, not an AttributeError crash.
    with pytest.raises(ValueError, match=r"manifest\.cells must be a mapping \(got list\)"):
        vm.validate_manifest(manifest, repo_root=repo_root)


def test_missing_study_id_raises(repo_root: Path) -> None:
    # Given
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    manifest = {"cells": {"A1": {"group": "A", "runner": "aris", "config": "x.yaml"}}}

    # When/Then
    with pytest.raises(ValueError, match=r"manifest\.study_id missing"):
        vm.validate_manifest(manifest, repo_root=repo_root)


# -----------------------------
# CLI entry-point (main)
# -----------------------------


def test_main_with_study_arg_returns_zero_on_success(repo_root: Path) -> None:
    # Given: a single fully-valid study.
    _write_groups(repo_root, {"A": "Claude Code CLI", "B": "Our System"})
    study_id = "ok-cli-study"
    _write_study(
        repo_root,
        study_id=study_id,
        manifest=_well_formed_manifest(study_id),
        config_files=["configs/A1.yaml", "configs/B1.yaml"],
    )

    # When/Then
    assert vm.main(["--study", study_id]) == 0


def test_main_with_study_arg_returns_one_on_failure(repo_root: Path) -> None:
    # Given: study that references an unknown group.
    _write_groups(repo_root, {"A": "Claude Code CLI"})
    study_id = "broken-cli-study"
    _write_study(
        repo_root,
        study_id=study_id,
        manifest={
            "study_id": study_id,
            "cells": {"Z1": {"group": "Z", "runner": "aris", "config": "configs/Z1.yaml"}},
        },
        config_files=["configs/Z1.yaml"],
    )

    # When/Then
    assert vm.main(["--study", study_id]) == 1


def test_main_returns_one_when_no_studies_found(repo_root: Path) -> None:
    # Given: experiments/ exists but contains nothing discoverable.
    (repo_root / "experiments").mkdir(parents=True, exist_ok=True)

    # When/Then
    assert vm.main([]) == 1


def test_main_without_args_validates_all_discovered_studies(repo_root: Path) -> None:
    # Given: two valid studies + the shared/ dir which must be skipped.
    _write_groups(repo_root, {"A": "Claude Code CLI", "B": "Our System"})
    for sid in ("study-a", "study-b"):
        _write_study(
            repo_root,
            study_id=sid,
            manifest=_well_formed_manifest(sid),
            config_files=["configs/A1.yaml", "configs/B1.yaml"],
        )

    # When/Then: discovery picks up both studies and validates them green.
    assert vm.main([]) == 0


def test_main_returns_one_if_any_study_fails(repo_root: Path) -> None:
    # Given: one good study and one with an unknown runner.
    _write_groups(repo_root, {"A": "Claude Code CLI", "B": "Our System"})
    _write_study(
        repo_root,
        study_id="good",
        manifest=_well_formed_manifest("good"),
        config_files=["configs/A1.yaml", "configs/B1.yaml"],
    )
    _write_study(
        repo_root,
        study_id="bad",
        manifest={
            "study_id": "bad",
            "cells": {"A1": {"group": "A", "runner": "ghost", "config": "configs/A1.yaml"}},
        },
        config_files=["configs/A1.yaml"],
    )

    # When/Then
    assert vm.main([]) == 1
