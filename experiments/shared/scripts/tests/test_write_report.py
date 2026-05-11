"""Tests for `experiments.shared.scripts.write_report`."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
import yaml

from experiments.shared.scripts.write_report import (
    GENERATED_JSON,
    write_binary,
    write_md,
)


if TYPE_CHECKING:
    from pathlib import Path


STUDY = "experiments/2026-01-01-fake-study"
CSV_PATH = f"{STUDY}/reports/tables/summary.csv"
RENDER_PATH = f"{STUDY}/scripts/render.py"
PLOT_PATH = f"{STUDY}/scripts/plot.py"
TEMPLATE_PATH = f"{STUDY}/templates/report.md.j2"


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_input(repo_root: Path, rel: str, content: bytes) -> Path:
    absolute = repo_root / rel
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(content)
    return absolute


def test_write_md_emits_frontmatter_and_body(repo_root: Path) -> None:
    # Given: an input CSV + a script file on disk.
    _write_input(repo_root, CSV_PATH, b"col\n1\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/scripts/render.py", b"# render\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/templates/report.md.j2", b"# tpl\n")

    target = repo_root / "experiments/2026-01-01-fake-study/reports/report.md"

    # When: writing the markdown report.
    write_md(
        path="experiments/2026-01-01-fake-study/reports/report.md",
        content="# Report body\n\nHello.\n",
        script="experiments/2026-01-01-fake-study/scripts/render.py",
        template="experiments/2026-01-01-fake-study/templates/report.md.j2",
        inputs=[CSV_PATH],
    )

    # Then: the file exists and starts with a YAML frontmatter block.
    text = target.read_text()
    assert text.startswith("---\n")
    _, frontmatter_block, body = text.split("---", 2)
    meta = yaml.safe_load(frontmatter_block)

    # And: required keys are populated.
    assert meta["generated_by"] == "experiments/2026-01-01-fake-study/scripts/render.py"
    assert meta["template"] == "experiments/2026-01-01-fake-study/templates/report.md.j2"
    assert meta["inputs"] == [
        CSV_PATH,
    ]
    # inputs_sha256 is a SUPERSET of inputs — it also carries the template
    # hash so the validator catches Jinja swaps.
    assert set(meta["inputs_sha256"]) == set(meta["inputs"]) | {meta["template"]}
    assert meta["inputs_sha256"][meta["inputs"][0]] == _sha256_bytes(b"col\n1\n")
    assert meta["inputs_sha256"][meta["template"]] == _sha256_bytes(b"# tpl\n")
    assert meta["generated_at"].endswith("Z")
    # And: the body is preserved.
    assert body.lstrip().startswith("# Report body")


def test_write_md_raises_when_input_missing(repo_root: Path) -> None:  # noqa: ARG001
    # Given: no CSV on disk.
    target_rel = "experiments/2026-01-01-fake-study/reports/report.md"

    # When/Then
    with pytest.raises(FileNotFoundError):
        write_md(
            path=target_rel,
            content="# body\n",
            script="experiments/2026-01-01-fake-study/scripts/render.py",
            template="experiments/2026-01-01-fake-study/templates/report.md.j2",
            inputs=["experiments/2026-01-01-fake-study/reports/tables/missing.csv"],
        )


def test_write_binary_creates_sidecar_entry(repo_root: Path) -> None:
    # Given: a CSV input and a script on disk.
    _write_input(repo_root, CSV_PATH, b"col\n1\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/scripts/plot.py", b"# plot\n")
    png_bytes = b"\x89PNG\r\n\x1a\n-fake-image-"

    # When: writing the PNG via write_binary.
    write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/01-success-rate.png",
        content=png_bytes,
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=[CSV_PATH],
    )

    # Then: the PNG is written verbatim.
    png_path = repo_root / "experiments/2026-01-01-fake-study/reports/figures/01-success-rate.png"
    assert png_path.read_bytes() == png_bytes

    # And: the sidecar at the REPORTS root (not per-subdirectory) indexes it.
    sidecar_path = (
        repo_root / "experiments/2026-01-01-fake-study/reports" / GENERATED_JSON
    )
    data = json.loads(sidecar_path.read_text())
    key = "experiments/2026-01-01-fake-study/reports/figures/01-success-rate.png"
    assert key in data
    entry = data[key]
    assert entry["generated_by"] == "experiments/2026-01-01-fake-study/scripts/plot.py"
    assert entry["inputs"] == [CSV_PATH]
    assert entry["inputs_sha256"][entry["inputs"][0]] == _sha256_bytes(b"col\n1\n")
    assert entry["output_sha256"] == _sha256_bytes(png_bytes)
    assert entry["generated_at"].endswith("Z")


def test_write_binary_raises_on_corrupt_sidecar(repo_root: Path) -> None:
    # Given: a sidecar that has been corrupted out-of-band after a prior write.
    _write_input(repo_root, CSV_PATH, b"a\n1\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/scripts/plot.py", b"# plot\n")
    write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/a.png",
        content=b"A",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=[CSV_PATH],
    )
    sidecar_path = (
        repo_root / "experiments/2026-01-01-fake-study/reports" / GENERATED_JSON
    )
    sidecar_path.write_bytes(b"not json{")
    new_binary = repo_root / "experiments/2026-01-01-fake-study/reports/figures/b.png"
    assert not new_binary.exists()

    # When/Then: the next write fails closed (audit N-9 — pre-fix the sidecar
    # would silently reset, orphaning the prior entry).
    with pytest.raises(ValueError, match="not valid JSON"):
        write_binary(
            path="experiments/2026-01-01-fake-study/reports/figures/b.png",
            content=b"B",
            script="experiments/2026-01-01-fake-study/scripts/plot.py",
            inputs=[CSV_PATH],
        )

    # And: the new binary did NOT land on disk (write order ensures the
    # sidecar check happens BEFORE the binary write).
    assert not new_binary.exists()


def test_write_binary_upserts_without_dropping_prior_entries(repo_root: Path) -> None:
    # Given: a CSV + an existing PNG entry in the sidecar.
    _write_input(repo_root, CSV_PATH, b"a\n1\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/scripts/plot.py", b"# plot\n")

    write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/a.png",
        content=b"A",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=[CSV_PATH],
    )

    # When: writing a second file in the same reports dir.
    write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/b.png",
        content=b"B",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=[CSV_PATH],
    )

    # Then: both entries coexist in the sidecar (at the reports root).
    sidecar = repo_root / "experiments/2026-01-01-fake-study/reports" / GENERATED_JSON
    data = json.loads(sidecar.read_text())
    assert "experiments/2026-01-01-fake-study/reports/figures/a.png" in data
    assert "experiments/2026-01-01-fake-study/reports/figures/b.png" in data


def test_write_binary_concurrent_upserts_are_serialized(repo_root: Path) -> None:
    # Given: a CSV input and a script on disk.
    _write_input(repo_root, CSV_PATH, b"a\n1\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/scripts/plot.py", b"# plot\n")

    def _emit(index: int) -> None:
        write_binary(
            path=f"experiments/2026-01-01-fake-study/reports/figures/{index:03d}.png",
            content=f"{index}".encode(),
            script="experiments/2026-01-01-fake-study/scripts/plot.py",
            inputs=[CSV_PATH],
        )

    # When: many writers race in parallel threads.
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_emit, range(40)))

    # Then: every entry appears in the final sidecar without corruption.
    sidecar = repo_root / "experiments/2026-01-01-fake-study/reports" / GENERATED_JSON
    data = json.loads(sidecar.read_text())
    assert len(data) == 40
    for idx in range(40):
        key = f"experiments/2026-01-01-fake-study/reports/figures/{idx:03d}.png"
        assert key in data


def test_write_md_rejects_outside_repo(repo_root: Path) -> None:
    # Given: a valid input inside the repo.
    _write_input(repo_root, CSV_PATH, b"a\n1\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/scripts/render.py", b"# render\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/templates/report.md.j2", b"# tpl\n")

    # When/Then: writing to a target outside the repo root raises.
    with pytest.raises(ValueError, match="outside the repository root"):
        write_md(
            path="/tmp/outside-report.md",  # noqa: S108 - deliberate outside-path
            content="# body\n",
            script="experiments/2026-01-01-fake-study/scripts/render.py",
            template="experiments/2026-01-01-fake-study/templates/report.md.j2",
            inputs=[CSV_PATH],
        )


def test_write_md_hash_matches_file_bytes_with_crlf_body(repo_root: Path) -> None:
    """Body bytes with CRLF line endings must hash to the exact on-disk bytes.

    Previously the writer went string -> read back via text mode (which on
    some platforms could normalize newlines) and re-hashed. With the bytes
    pipeline, `output_sha256` matches the body bytes byte-for-byte including
    every `\\r\\n` sequence.
    """
    # Given: a CSV, script, and template on disk.
    _write_input(repo_root, CSV_PATH, b"col\n1\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/scripts/render.py", b"# render\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/templates/report.md.j2", b"# tpl\n")
    crlf_body = "# Report\r\n\r\nLine one.\r\nLine two.\r\n"

    # When: writing the markdown report.
    md_path = write_md(
        path="experiments/2026-01-01-fake-study/reports/report.md",
        content=crlf_body,
        script="experiments/2026-01-01-fake-study/scripts/render.py",
        template="experiments/2026-01-01-fake-study/templates/report.md.j2",
        inputs=[CSV_PATH],
    )

    # Then: the exact body bytes (including `\r\n`) are preserved on disk and
    # `output_sha256` matches those bytes directly.
    raw = md_path.read_bytes()
    # Split at the closing fence plus the one-newline separator the writer
    # inserts: `---\n<yaml>---\n\n<body>`.
    assert raw.startswith(b"---\n")
    close_marker = b"\n---\n\n"
    split_idx = raw.find(close_marker)
    assert split_idx != -1, "closing fence + separator not found"
    body_bytes = raw[split_idx + len(close_marker):]
    assert b"\r\n" in body_bytes, "CRLF sequences must be preserved byte-for-byte"

    # And: frontmatter's output_sha256 matches those body bytes exactly.
    yaml_block = raw[len(b"---\n"):split_idx + 1]
    meta = yaml.safe_load(yaml_block)
    expected = hashlib.sha256(body_bytes).hexdigest()
    assert meta["output_sha256"] == expected


def test_write_md_outside_reports_raises_before_any_write(repo_root: Path) -> None:
    """A misaimed target must fail BEFORE any file is written."""
    # Given: valid inputs for an outside-reports path.
    _write_input(repo_root, CSV_PATH, b"col\n1\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/scripts/render.py", b"# render\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/templates/report.md.j2", b"# tpl\n")
    bogus_rel = "experiments/2026-01-01-fake-study/outside_reports/report.md"
    target_abs = repo_root / bogus_rel

    # When/Then: writer raises.
    with pytest.raises(ValueError, match="not under any"):
        write_md(
            path=bogus_rel,
            content="# body\n",
            script="experiments/2026-01-01-fake-study/scripts/render.py",
            template="experiments/2026-01-01-fake-study/templates/report.md.j2",
            inputs=[CSV_PATH],
        )

    # And: no stray file was written at the target or its parent directory.
    assert not target_abs.exists()
    assert not target_abs.parent.exists()


def test_write_binary_outside_reports_raises_before_any_write(repo_root: Path) -> None:
    """Same guard for `write_binary`: no stray artifacts outside reports/."""
    _write_input(repo_root, CSV_PATH, b"col\n1\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/scripts/plot.py", b"# plot\n")
    bogus_rel = "experiments/2026-01-01-fake-study/outside_reports/fig.png"
    target_abs = repo_root / bogus_rel

    with pytest.raises(ValueError, match="not under any"):
        write_binary(
            path=bogus_rel,
            content=b"\x89PNG\r\n",
            script="experiments/2026-01-01-fake-study/scripts/plot.py",
            inputs=[CSV_PATH],
        )

    assert not target_abs.exists()
    assert not target_abs.parent.exists()


def test_write_binary_does_not_leave_lockfile_in_reports(repo_root: Path) -> None:
    """The `.generated.json.lock` leftover must NOT appear in reports/.

    The lock lives in the system tempdir. reports/ must contain exactly the
    generated artifact and the sidecar — the validator does not need to
    special-case a transient lock.
    """
    _write_input(repo_root, CSV_PATH, b"col\n1\n")
    _write_input(repo_root, "experiments/2026-01-01-fake-study/scripts/plot.py", b"# plot\n")

    write_binary(
        path="experiments/2026-01-01-fake-study/reports/figures/01.png",
        content=b"bytes",
        script="experiments/2026-01-01-fake-study/scripts/plot.py",
        inputs=[CSV_PATH],
    )

    reports_dir = repo_root / "experiments/2026-01-01-fake-study/reports"
    all_files = {p.name for p in reports_dir.rglob("*") if p.is_file()}
    assert ".generated.json.lock" not in all_files
    # Sanity: the real sidecar is there, so the upsert really ran.
    assert GENERATED_JSON in all_files
