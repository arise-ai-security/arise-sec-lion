"""Tests for `experiments.shared.scripts.validate_reports`.

The single most important test here is ``test_hand_edited_output_sha256
_rejected`` — it proves that forging a matching sidecar entry without also
regenerating the output file is caught, which is the whole point of the
scripts-first enforcement (spec §7.3).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from experiments.shared.scripts.validate_reports import main, validate_paths
from experiments.shared.scripts.write_report import GENERATED_JSON, write_binary, write_md


if TYPE_CHECKING:
    from pathlib import Path


def _seed_inputs(repo_root: Path) -> None:
    (repo_root / "experiments/2026-01-01-fake-study/reports/tables").mkdir(parents=True)
    (repo_root / "experiments/2026-01-01-fake-study/scripts").mkdir(parents=True)
    (repo_root / "experiments/2026-01-01-fake-study/templates").mkdir(parents=True)

    (repo_root / "experiments/2026-01-01-fake-study/reports/tables/summary.csv").write_bytes(
        b"col\n1\n"
    )
    (repo_root / "experiments/2026-01-01-fake-study/scripts/render.py").write_bytes(b"# render\n")
    (repo_root / "experiments/2026-01-01-fake-study/scripts/plot.py").write_bytes(b"# plot\n")
    (repo_root / "experiments/2026-01-01-fake-study/templates/report.md.j2").write_bytes(b"# tpl\n")


def test_valid_markdown_report_passes(repo_root: Path) -> None:
    # Given: a freshly-written markdown report with matching input.
    _seed_inputs(repo_root)
    md_path = write_md(
        path="experiments/2026-01-01-fake-study/reports/report.md",
        content="# body\n",
        script="experiments/2026-01-01-fake-study/scripts/render.py",
        template="experiments/2026-01-01-fake-study/templates/report.md.j2",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )

    # When
    errors = validate_paths([md_path])

    # Then
    assert errors == []


def test_markdown_without_frontmatter_is_rejected(repo_root: Path) -> None:
    # Given: a markdown file with no frontmatter.
    _seed_inputs(repo_root)
    md_path = repo_root / "experiments/2026-01-01-fake-study/reports/report.md"
    md_path.write_text("# hand-written\n")

    # When
    errors = validate_paths([md_path])

    # Then
    assert len(errors) == 1
    assert "missing or malformed YAML frontmatter" in errors[0]


def test_markdown_with_stale_input_sha_is_rejected(repo_root: Path) -> None:
    # Given: a valid markdown report that then has its input silently
    # edited after the fact (simulating drift).
    _seed_inputs(repo_root)
    md_path = write_md(
        path="experiments/2026-01-01-fake-study/reports/report.md",
        content="# body\n",
        script="experiments/2026-01-01-fake-study/scripts/render.py",
        template="experiments/2026-01-01-fake-study/templates/report.md.j2",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )
    csv = repo_root / "experiments/2026-01-01-fake-study/reports/tables/summary.csv"
    csv.write_bytes(b"col\n99\n")  # drift

    # When
    errors = validate_paths([md_path])

    # Then
    assert len(errors) == 1
    assert "sha256 mismatch for input" in errors[0]


def test_binary_with_valid_sidecar_passes(repo_root: Path) -> None:
    # Given
    _seed_inputs(repo_root)
    png = write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/01.png",
        content=b"\x89PNG\r\n\x1a\n-fake-",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )

    # When
    errors = validate_paths([png])

    # Then
    assert errors == []


def test_binary_without_sidecar_entry_is_rejected(repo_root: Path) -> None:
    # Given: a binary file dropped into reports/ without any sidecar.
    _seed_inputs(repo_root)
    png = repo_root / "experiments/2026-01-01-fake-study/reports/figures/rogue.png"
    png.parent.mkdir(parents=True)
    png.write_bytes(b"rogue")

    # When
    errors = validate_paths([png])

    # Then
    assert len(errors) == 1
    assert "no `.generated.json` entry" in errors[0]


def test_hand_edited_output_sha256_rejected(repo_root: Path) -> None:
    """The canary test: tampering with the sidecar to cover a rewrite is caught."""
    # Given: a valid binary + sidecar.
    _seed_inputs(repo_root)
    png = write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/01.png",
        content=b"authentic",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )

    # When: the forger hand-edits `output_sha256` in the sidecar to a bogus
    # value while leaving the output file untouched. A validator that only
    # checked inputs would miss this; the spec §7.3 requires re-hashing the
    # output and comparing to the stored value.
    sidecar = repo_root / "experiments/2026-01-01-fake-study/reports" / GENERATED_JSON
    data = json.loads(sidecar.read_text())
    key = "experiments/2026-01-01-fake-study/reports/figures/01.png"
    data[key]["output_sha256"] = "0" * 64  # forged
    sidecar.write_text(json.dumps(data, indent=2, sort_keys=True))

    errors = validate_paths([png])
    assert len(errors) == 1
    assert "sha256 mismatch for output" in errors[0]


def test_binary_with_stale_input_sha_is_rejected(repo_root: Path) -> None:
    # Given: a valid binary/sidecar, then the input file drifts.
    _seed_inputs(repo_root)
    png = write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/01.png",
        content=b"bytes",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )
    (repo_root / "experiments/2026-01-01-fake-study/reports/tables/summary.csv").write_bytes(
        b"drifted\n"
    )

    # When
    errors = validate_paths([png])

    # Then
    assert len(errors) == 1
    assert "sha256 mismatch for input" in errors[0]


def test_generated_json_is_self_exception(repo_root: Path) -> None:
    # Given: a valid binary whose presence creates `.generated.json` at the
    # REPORTS ROOT (not inside the figures/ subdir — see spec §7.1).
    _seed_inputs(repo_root)
    write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/01.png",
        content=b"x",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )
    sidecar = repo_root / "experiments/2026-01-01-fake-study/reports" / GENERATED_JSON
    assert sidecar.is_file()

    # When: the validator is asked to check the sidecar itself.
    errors = validate_paths([sidecar])

    # Then: it's skipped rather than flagged.
    assert errors == []


def test_generated_json_lookalike_is_not_skipped(repo_root: Path) -> None:
    """Only the literal `.generated.json` is the validator's self-exception.

    Files that merely START with the sidecar name (e.g. a hand-planted
    `.generated.json.malicious`) must be validated like any other file.
    """
    _seed_inputs(repo_root)
    write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/01.png",
        content=b"x",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )
    # Plant a sneaky lookalike file alongside the real sidecar.
    stray = (
        repo_root
        / "experiments/2026-01-01-fake-study/reports"
        / f"{GENERATED_JSON}.malicious"
    )
    stray.write_bytes(b"{}")

    errors = validate_paths([stray])
    # Then: it's NOT skipped — it fails because it has no sidecar entry.
    assert len(errors) == 1
    assert "no `.generated.json` entry" in errors[0]


def test_markdown_body_tamper_is_rejected(repo_root: Path) -> None:
    """Edits to the rendered markdown body after write are caught by output_sha256."""
    # Given: a valid markdown report.
    _seed_inputs(repo_root)
    md = write_md(
        path="experiments/2026-01-01-fake-study/reports/report.md",
        content="# original body\n",
        script="experiments/2026-01-01-fake-study/scripts/render.py",
        template="experiments/2026-01-01-fake-study/templates/report.md.j2",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )

    # When: a human edits the body while leaving the frontmatter intact.
    text = md.read_text()
    frontmatter, _original_body = text.split("---", 2)[1:]
    tampered = f"---{frontmatter}---\n# TAMPERED body\n"
    md.write_text(tampered)

    errors = validate_paths([md])
    assert len(errors) == 1
    assert "sha256 mismatch for markdown body" in errors[0]


def test_mixed_directory_reports_only_offenders(repo_root: Path) -> None:
    # Given: a mix of valid and invalid markdowns.
    _seed_inputs(repo_root)
    good = write_md(
        path="experiments/2026-01-01-fake-study/reports/report.md",
        content="# body\n",
        script="experiments/2026-01-01-fake-study/scripts/render.py",
        template="experiments/2026-01-01-fake-study/templates/report.md.j2",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )
    bad = repo_root / "experiments/2026-01-01-fake-study/reports/bad.md"
    bad.write_text("# hand-written\n")

    # When
    errors = validate_paths([good, bad])

    # Then: only the bad file is reported.
    assert len(errors) == 1
    assert errors[0].startswith("experiments/2026-01-01-fake-study/reports/bad.md:")


def test_main_returns_nonzero_on_violation(repo_root: Path) -> None:
    # Given: a bad report.
    _seed_inputs(repo_root)
    bad = repo_root / "experiments/2026-01-01-fake-study/reports/bad.md"
    bad.write_text("# hand-written\n")

    # When
    code = main([str(bad)])

    # Then
    assert code == 1


def test_main_returns_zero_on_empty_target_list(repo_root: Path) -> None:  # noqa: ARG001
    # The repo_root fixture re-routes REPO_ROOT to a clean tmp dir so main()
    # sees no report files and returns zero.
    assert main([]) == 0


def test_file_outside_reports_is_rejected(repo_root: Path) -> None:
    # Given: a file not under experiments/<study>/reports/
    _seed_inputs(repo_root)
    stray = repo_root / "experiments/2026-01-01-fake-study/scripts/render.py"

    # When
    errors = validate_paths([stray])

    # Then
    assert len(errors) == 1
    assert "not under any" in errors[0]


def test_validate_accepts_absolute_paths(repo_root: Path) -> None:
    _seed_inputs(repo_root)
    md = write_md(
        path="experiments/2026-01-01-fake-study/reports/report.md",
        content="# body\n",
        script="experiments/2026-01-01-fake-study/scripts/render.py",
        template="experiments/2026-01-01-fake-study/templates/report.md.j2",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )
    # When: caller passes an absolute path (pre-commit will).
    errors = validate_paths([md.resolve()])
    # Then
    assert errors == []


@pytest.mark.parametrize(
    "mutator",
    [
        lambda meta: meta.pop("inputs_sha256"),
        lambda meta: meta.pop("inputs"),
        lambda meta: meta.pop("generated_by"),
        lambda meta: meta.pop("generated_at"),
        lambda meta: meta.pop("output_sha256"),
        lambda meta: meta.pop("template"),
    ],
)
def test_markdown_missing_required_keys_is_rejected(repo_root: Path, mutator) -> None:
    """Each removal of a required key triggers a distinct error."""
    # Given: a valid markdown file whose frontmatter is then tampered.
    _seed_inputs(repo_root)
    import yaml

    md = write_md(
        path="experiments/2026-01-01-fake-study/reports/report.md",
        content="# body\n",
        script="experiments/2026-01-01-fake-study/scripts/render.py",
        template="experiments/2026-01-01-fake-study/templates/report.md.j2",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )
    text = md.read_text()
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm)
    mutator(meta)
    md.write_text(f"---\n{yaml.safe_dump(meta, sort_keys=True)}---{body}")

    # When
    errors = validate_paths([md])

    # Then
    assert len(errors) == 1
    assert "missing required keys" in errors[0]


def test_sidecar_with_absolute_input_path_is_rejected(repo_root: Path) -> None:
    """A sidecar entry recording an absolute path (e.g. `/etc/passwd`) must be
    rejected even if its sha256 happens to match — validation never trusts a
    recorded path as-is."""
    _seed_inputs(repo_root)
    png = write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/01.png",
        content=b"bytes",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )

    # Tamper with the sidecar: rewrite `inputs` to include an absolute path.
    sidecar = repo_root / "experiments/2026-01-01-fake-study/reports" / GENERATED_JSON
    data = json.loads(sidecar.read_text())
    key = "experiments/2026-01-01-fake-study/reports/figures/01.png"
    bad_abs = "/etc/passwd"
    data[key]["inputs"] = [bad_abs]
    data[key]["inputs_sha256"] = {bad_abs: "0" * 64}
    sidecar.write_text(json.dumps(data, indent=2, sort_keys=True))

    errors = validate_paths([png])
    assert any("must be relative" in e for e in errors), errors


def test_sidecar_with_dot_dot_path_is_rejected(repo_root: Path) -> None:
    """Paths containing `..` segments must be rejected even if they resolve
    back inside the repo — the strict rule is the simplest to audit."""
    _seed_inputs(repo_root)
    png = write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/01.png",
        content=b"bytes",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )

    sidecar = repo_root / "experiments/2026-01-01-fake-study/reports" / GENERATED_JSON
    data = json.loads(sidecar.read_text())
    key = "experiments/2026-01-01-fake-study/reports/figures/01.png"
    bad = "experiments/2026-01-01-fake-study/reports/../../../etc/passwd"
    data[key]["inputs"] = [bad]
    data[key]["inputs_sha256"] = {bad: "0" * 64}
    sidecar.write_text(json.dumps(data, indent=2, sort_keys=True))

    errors = validate_paths([png])
    assert any("must not contain '..'" in e for e in errors), errors


def test_sidecar_generated_by_outside_repo_is_rejected(repo_root: Path) -> None:
    """`generated_by` is just as trust-sensitive as any other recorded path.

    A sidecar forged to point at `/usr/bin/python` must be rejected for the
    same reason `inputs` entries are.
    """
    _seed_inputs(repo_root)
    png = write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/01.png",
        content=b"bytes",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=["experiments/2026-01-01-fake-study/reports/tables/summary.csv"],
    )

    sidecar = repo_root / "experiments/2026-01-01-fake-study/reports" / GENERATED_JSON
    data = json.loads(sidecar.read_text())
    key = "experiments/2026-01-01-fake-study/reports/figures/01.png"
    data[key]["generated_by"] = "/usr/bin/python"
    sidecar.write_text(json.dumps(data, indent=2, sort_keys=True))

    errors = validate_paths([png])
    assert any("generated_by" in e and "must be relative" in e for e in errors), errors
