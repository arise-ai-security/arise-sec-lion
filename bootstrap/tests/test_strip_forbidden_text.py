"""Unit tests for ``bootstrap.composition._strip_forbidden_text``.

End-to-end tests in
``plugins/security/tests/test_prompt_unification_invariants.py`` prove the
strip works through the full pipeline. These direct unit tests localize
regressions in the helper itself — invalid JSON handling, the
byte-identity fast path, and the strip output.
"""

from __future__ import annotations

import json

import pytest

from bootstrap.composition import _strip_forbidden_text


def test_returns_none_for_none_input() -> None:
    assert _strip_forbidden_text(None) is None


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"unterminated":',
        "",
    ],
)
def test_returns_none_for_invalid_json(raw: str) -> None:
    assert _strip_forbidden_text(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "[1,2,3]",
        '"a string"',
        "42",
        "true",
        "null",
    ],
)
def test_returns_none_for_non_object_json(raw: str) -> None:
    assert _strip_forbidden_text(raw) is None


def test_benign_dict_returns_input_verbatim() -> None:
    raw = '{"cve_id":"CVE-X","extra":"raw"}\n'
    assert _strip_forbidden_text(raw) is raw


def test_empty_dict_returns_input_verbatim() -> None:
    raw = "{}"
    assert _strip_forbidden_text(raw) is raw


def test_strips_patch_field() -> None:
    raw = json.dumps({"cve_id": "CVE-X", "patch": "diff --git a/x b/x"})
    out = _strip_forbidden_text(raw)
    assert out is not None
    parsed = json.loads(out)
    assert "patch" not in parsed
    assert parsed["cve_id"] == "CVE-X"


def test_strips_candidate_fixes_field() -> None:
    raw = json.dumps({"cve_id": "CVE-X", "candidate_fixes": "alternative-fix"})
    out = _strip_forbidden_text(raw)
    assert out is not None
    parsed = json.loads(out)
    assert "candidate_fixes" not in parsed
    assert parsed["cve_id"] == "CVE-X"


def test_strips_both_forbidden_fields_simultaneously() -> None:
    raw = json.dumps(
        {
            "cve_id": "CVE-X",
            "patch": "diff --git a/x b/x",
            "candidate_fixes": "alt",
            "bug_report": "kept",
        }
    )
    out = _strip_forbidden_text(raw)
    assert out is not None
    parsed = json.loads(out)
    assert "patch" not in parsed
    assert "candidate_fixes" not in parsed
    assert parsed["bug_report"] == "kept"
    assert parsed["cve_id"] == "CVE-X"


def test_strips_patch_when_value_is_null() -> None:
    raw = json.dumps({"cve_id": "CVE-X", "patch": None})
    out = _strip_forbidden_text(raw)
    assert out is not None
    assert "patch" not in json.loads(out)


def test_strip_path_preserves_non_ascii() -> None:
    payload = {"cve_id": "CVE-X", "desc": "Ünïçødé bug", "patch": "diff"}
    raw = json.dumps(payload, ensure_ascii=False)
    out = _strip_forbidden_text(raw)
    assert out is not None
    # ensure_ascii=False keeps non-ASCII bytes identical between fast
    # path and strip path.
    assert "Ünïçødé" in out
    assert "patch" not in json.loads(out)
