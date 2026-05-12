"""Tests for ``infrastructure.io.atomic_write``."""

from __future__ import annotations

import json
from pathlib import Path

from infrastructure.io.atomic_write import write_bytes, write_json, write_text


def test_write_text_writes_expected_content(tmp_path: Path) -> None:
    # Given: a destination path inside tmp_path.
    target = tmp_path / "note.txt"

    # When: write_text is called with a unicode payload.
    write_text(target, "hello, world\nsecond line\n")

    # Then: the file exists and round-trips exactly.
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "hello, world\nsecond line\n"


def test_write_bytes_writes_expected_content(tmp_path: Path) -> None:
    # Given: a destination path and a binary payload.
    target = tmp_path / "blob.bin"
    payload = b"\x00\x01\x02\xff"

    # When: write_bytes is called.
    write_bytes(target, payload)

    # Then: the file exists and contains the exact bytes.
    assert target.is_file()
    assert target.read_bytes() == payload


def test_write_json_writes_expected_content(tmp_path: Path) -> None:
    # Given: a JSON-serializable object and a destination.
    target = tmp_path / "data.json"
    obj = {"alpha": 1, "beta": [True, False, None], "gamma": "ok"}

    # When: write_json is called.
    write_json(target, obj)

    # Then: the file decodes back to the same Python object.
    decoded = json.loads(target.read_text(encoding="utf-8"))
    assert decoded == obj

    # And: the output ends with a trailing newline (POSIX text convention).
    assert target.read_bytes().endswith(b"\n")


def test_write_text_creates_missing_parent_dirs(tmp_path: Path) -> None:
    # Given: a deeply nested path whose parents do not yet exist.
    target = tmp_path / "a" / "b" / "c" / "note.txt"
    assert not target.parent.exists()

    # When: write_text is called.
    write_text(target, "nested")

    # Then: the parent chain has been created and the file is present.
    assert target.parent.is_dir()
    assert target.read_text(encoding="utf-8") == "nested"


def test_write_bytes_creates_missing_parent_dirs(tmp_path: Path) -> None:
    # Given: a nested path whose parents are missing.
    target = tmp_path / "x" / "y" / "blob.bin"
    assert not target.parent.exists()

    # When: write_bytes is called.
    write_bytes(target, b"payload")

    # Then: parent chain is created and content matches.
    assert target.parent.is_dir()
    assert target.read_bytes() == b"payload"


def test_write_json_creates_missing_parent_dirs(tmp_path: Path) -> None:
    # Given: a nested path whose parents are missing.
    target = tmp_path / "deep" / "tree" / "data.json"
    assert not target.parent.exists()

    # When: write_json is called.
    write_json(target, {"k": "v"})

    # Then: parent chain is created and the JSON round-trips.
    assert target.parent.is_dir()
    assert json.loads(target.read_text(encoding="utf-8")) == {"k": "v"}


def test_write_text_replaces_existing_content_atomically(tmp_path: Path) -> None:
    # Given: a file that already has previous content.
    target = tmp_path / "rewrite.txt"
    target.write_text("old content", encoding="utf-8")

    # When: write_text overwrites it.
    write_text(target, "new content")

    # Then: the post-condition is the new content only — no merge, no leftover.
    assert target.read_text(encoding="utf-8") == "new content"


def test_write_bytes_replaces_existing_content_atomically(tmp_path: Path) -> None:
    # Given: a file with existing bytes.
    target = tmp_path / "rewrite.bin"
    target.write_bytes(b"old")

    # When: write_bytes replaces it.
    write_bytes(target, b"new payload")

    # Then: the file now holds only the new bytes.
    assert target.read_bytes() == b"new payload"


def test_write_text_leaves_no_tmp_files_on_success(tmp_path: Path) -> None:
    # Given: a directory empty of files.
    target = tmp_path / "clean.txt"
    assert list(tmp_path.iterdir()) == []

    # When: write_text completes successfully.
    write_text(target, "ok")

    # Then: only the target file remains — no ``*.tmp`` strays in the parent.
    entries = list(tmp_path.iterdir())
    assert entries == [target]
    assert not any(p.name.endswith(".tmp") for p in entries)


def test_write_bytes_leaves_no_tmp_files_on_success(tmp_path: Path) -> None:
    # Given: a clean directory.
    target = tmp_path / "clean.bin"

    # When: write_bytes completes successfully.
    write_bytes(target, b"ok")

    # Then: no ``*.tmp`` files remain alongside.
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_write_json_leaves_no_tmp_files_on_success(tmp_path: Path) -> None:
    # Given: a clean directory.
    target = tmp_path / "clean.json"

    # When: write_json completes successfully.
    write_json(target, {"a": 1})

    # Then: no ``*.tmp`` files remain alongside.
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_write_text_with_fsync_false_does_not_raise(tmp_path: Path) -> None:
    # Given: a destination path.
    target = tmp_path / "nofsync.txt"

    # When: write_text is called with fsync disabled.
    write_text(target, "fast", fsync=False)

    # Then: the file is still written correctly (durability is the only thing dropped).
    assert target.read_text(encoding="utf-8") == "fast"


def test_write_bytes_with_fsync_false_does_not_raise(tmp_path: Path) -> None:
    # Given: a destination path.
    target = tmp_path / "nofsync.bin"

    # When: write_bytes is called with fsync disabled.
    write_bytes(target, b"fast", fsync=False)

    # Then: the bytes are present in the destination.
    assert target.read_bytes() == b"fast"


def test_write_json_with_fsync_false_does_not_raise(tmp_path: Path) -> None:
    # Given: a destination path.
    target = tmp_path / "nofsync.json"

    # When: write_json is called with fsync disabled.
    write_json(target, [1, 2, 3], fsync=False)

    # Then: the JSON round-trips.
    assert json.loads(target.read_text(encoding="utf-8")) == [1, 2, 3]
