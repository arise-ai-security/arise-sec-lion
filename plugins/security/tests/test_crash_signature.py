"""Tests for sanitizer crash-signature extraction."""

import json
from pathlib import Path

import pytest

from plugins.security.crash_signature import compute_crash_signature, signatures_match


_FIXTURES = Path(__file__).parent / "fixtures"


def _report(name: str) -> str:
    payload = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload["sanitizer_report"]


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        (
            "md4c.cve-2020-26148",
            ("use-of-uninitialized-value", "unknown", "md_push_block_bytes"),
        ),
        (
            "md4c.cve-2021-30027",
            ("use-of-uninitialized-value", "unknown", "md_analyze_line"),
        ),
        ("gpac.cve-2022-2454", ("undefined-behavior", "unknown", "lsr_dec.c:853")),
        (
            "gpac.cve-2023-42298",
            ("undefined-behavior", "unknown", "unquantize.c:298"),
        ),
        ("mruby.cve-2018-14337", ("undefined-behavior", "unknown", "sprintf.c:966")),
        (
            "opencv.issue-28598",
            ("undefined-behavior", "unknown", "cv::FillConvexPoly"),
        ),
        (
            "pytorch.issue-181510",
            ("undefined-behavior", "unknown", "vec256_float.h:125"),
        ),
        ("imagemagick.cve-2018-5247", ("memory-leak", "unknown", "ReadRLAImage")),
    ),
)
def test_non_asan_fixture_signature_is_exact(name, expected) -> None:
    # Given: A frozen MSan, UBSan, or LSan fixture report
    report = _report(name)

    # When: The runtime computes its crash signature
    signature = compute_crash_signature(report)

    # Then: Class, explicit unknown access, and stable application frame are exact
    assert signature.as_tuple() == expected
    assert signature.complete
    assert signatures_match(signature, signature)


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("exiv2.cve-2017-14857", ("bad-free", "unknown", "Exiv2::Image::~Image")),
        ("gpac.cve-2023-4679", ("double-free", "unknown", "gf_filterpacket_del")),
        (
            "gpac.cve-2021-40575",
            ("negative-size-param", "unknown", "mpgviddmx_process"),
        ),
        ("libplist.cve-2017-5545", ("heap-buffer-overflow", "read", "main")),
    ),
)
def test_asan_special_case_signature_is_exact(name, expected) -> None:
    # Given: An ASan fixture whose header or source path needs normalization
    report = _report(name)

    # When: The runtime computes its crash signature
    signature = compute_crash_signature(report)

    # Then: The ASan class and first application frame are not truncated or skipped
    assert signature.as_tuple() == expected
    assert signature.complete


def test_different_sanitizer_families_do_not_match() -> None:
    # Given: Complete MSan and UBSan signatures
    memory = compute_crash_signature(_report("md4c.cve-2020-26148"))
    undefined = compute_crash_signature(_report("gpac.cve-2022-2454"))

    # When: Their exact signatures are compared
    matched = signatures_match(memory, undefined)

    # Then: A common unknown-access category does not collapse distinct failures
    assert not matched


def test_missing_sanitizer_evidence_is_incomplete() -> None:
    # Given: Output without a sanitizer report
    output = "ordinary program output"

    # When: The runtime computes its crash signature
    signature = compute_crash_signature(output)

    # Then: It fails closed as an incomplete, non-crash signature
    assert signature.as_tuple() == (None, None, None)
    assert not signature.complete
    assert not signature.crashed


def test_all_nonempty_supported_fixture_reports_have_complete_signatures() -> None:
    # Given: Every frozen fixture that declares a supported sanitizer and has a report
    incomplete: list[str] = []

    # When: The runtime computes each exact signature
    for path in sorted(_FIXTURES.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        report = payload["sanitizer_report"]
        if payload["sanitizer"] == "none" or not report.strip():
            continue
        if not compute_crash_signature(report).complete:
            incomplete.append(path.name)

    # Then: No nonempty ASan, MSan, UBSan, or LSan oracle is incomplete
    assert incomplete == []
