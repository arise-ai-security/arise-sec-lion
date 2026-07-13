"""Tests for the deterministic crash-signature floor under the cve_reproduced judge.

The floor extracts (sanitizer_class, access_kind, top_application_frame) from sanitizer output
and credits a reproduction when the observed signature equals the golden's — correcting
stochastic LLM false-negatives. It must (a) skip sanitizer/interceptor/libc frames to the
project crash site, and (b) reject a real access-kind mismatch (the libredwg case).
"""

import json
from pathlib import Path

import yaml

from experiments.shared.evaluation.criteria import _crash_signature
from experiments.shared.evaluation.official import crash_signature


# Real ASan excerpts (trimmed) from the held-out runs.
_NJS_GOLDEN = """\
==181398==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000040 (pc 0x7f T0)
==181398==The signal is caused by a READ memory access.
    #0 0x7f in memcpy /build/glibc-2.27/string/../sysdeps/x86_64/multiarch/memmove.S:139
    #1 0x49 in __asan_memcpy /b/compiler-rt/lib/asan/asan_interceptors_memintrinsics.cpp:22:3
    #2 0x5e in njs_json_stringify_iterator /home/yongheng/njs/src/njs_json.c:1317:33
    #3 0x5e in njs_json_stringify /home/yongheng/njs/src/njs_json.c:283:12
"""
_NJS_B4_OBSERVED = """\
==4063==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000040 (pc 0x7f T0)
==4063==The signal is caused by a READ memory access.
    #0 0x7f in memcpy /build/glibc-2.31/string/../sysdeps/x86_64/multiarch/memmove.S:142
    #1 0x55 in __asan_memcpy /src/llvm-project/compiler-rt/lib/asan/asan_interceptors.cpp:63:3
    #2 0x55 in njs_json_stringify_iterator /src/njs/src/njs_json.c:1317:33
    #3 0x55 in njs_json_stringify /src/njs/src/njs_json.c:283:12
"""
_OPENJPEG_GOLDEN = """\
==159529==ERROR: AddressSanitizer: stack-buffer-overflow on address 0x7f at pc 0x45
WRITE of size 36 at 0x7f thread T0
    #0 0x45 in scanf_common /b/compiler-rt/lib/asan/../sanitizer_common/interceptors_format.inc:343
    #1 0x45 in __interceptor___isoc99_vfscanf /b/compiler-rt/lib/asan/../sanitizer_common/x.inc:1265
    #2 0x45 in __interceptor___isoc99_fscanf /b/compiler-rt/lib/asan/../sanitizer_common/x.inc:1282
    #3 0x52 in pgxtoimage /var/tmp/portage/media-libs/openjpeg-9999/work/.../convert.c:1188:9
"""
_LIBREDWG_GOLDEN_READ = """\
==1==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x50 at pc 0x44
READ of size 1 at 0x50 thread T0
    #0 0x44 in htmlescape /src/libredwg/programs/escape.c:42:7
"""
_LIBREDWG_B4_WRITE = """\
==1==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x50 at pc 0x44
WRITE of size 5 at 0x50 thread T0
    #0 0x44 in htmlescape /src/libredwg/programs/escape.c:42:7
"""


def test_signature_skips_interceptor_frames_to_app_frame() -> None:
    assert _crash_signature(_NJS_GOLDEN) == ("segv", "read", "njs_json_stringify_iterator")
    # openjpeg: must skip scanf_common/vfscanf/fscanf interceptors down to pgxtoimage
    assert _crash_signature(_OPENJPEG_GOLDEN) == ("stack-buffer-overflow", "write", "pgxtoimage")


def test_njs_observed_matches_golden_signature() -> None:
    assert _crash_signature(_NJS_B4_OBSERVED) == _crash_signature(_NJS_GOLDEN)


def test_access_kind_mismatch_is_distinguished() -> None:
    # Same class + same top frame, but READ (golden) vs WRITE (observed) -> NOT the same defect.
    g = _crash_signature(_LIBREDWG_GOLDEN_READ)
    o = _crash_signature(_LIBREDWG_B4_WRITE)
    assert g == ("heap-buffer-overflow", "read", "htmlescape")
    assert o == ("heap-buffer-overflow", "write", "htmlescape")
    assert g != o


def test_empty_and_garbage_yield_no_signature() -> None:
    assert _crash_signature("") == (None, None, None)
    assert _crash_signature("no sanitizer output here") == (None, None, None)


_FAAD2_SEGV_UNKNOWN = """\
==7076==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000 (pc 0x7f T0)
    #0 0x7f in ifilter_bank /root/faad2_asan/libfaad/filtbank.c:275
    #1 0x7f in reconstruct_channel_pair /root/faad2_asan/libfaad/specrec.c:1258

AddressSanitizer can not provide additional info.
SUMMARY: AddressSanitizer: SEGV /root/faad2_asan/libfaad/filtbank.c:275 ifilter_bank
"""

_MUPDF_SYMBOLLESS = """\
==2173==ERROR: AddressSanitizer: stack-buffer-overflow on address 0x7fff at pc 0x55
READ of size 4 at 0x7fff thread T0
    #0 0x5573684750d5  (/out/mupdf/mutool+0x45c0d5)
    #1 0x55736847109b  (/out/mupdf/mutool+0x45809b)
    #20 0x7f0824294082 in __libc_start_main /build/glibc-FcRMwW/glibc-2.31/csu/libc-start.c:308:16
SUMMARY: AddressSanitizer: stack-buffer-overflow (/out/mupdf/mutool+0x45c0d5)
"""


def test_segv_on_unknown_address_preregisters_unknown_access() -> None:
    # faad2.cve-2018-20362: class segv, frame ifilter_bank, access unstated by ASan.
    assert _crash_signature(_FAAD2_SEGV_UNKNOWN) == ("segv", "unknown", "ifilter_bank")


def test_symbol_less_trace_yields_module_relative_frame() -> None:
    # mupdf.ossfuzz-42520029: no symbolized function; use module+offset.
    assert _crash_signature(_MUPDF_SYMBOLLESS) == (
        "stack-buffer-overflow",
        "read",
        "mutool+0x45c0d5",
    )


_LIBREDWG_WITH_LIBASAN_OFFSET = """\
==9632==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x604000000abd
READ of size 10 at 0x604000000abd thread T0
    #0 0x7f1b8c9f066d  (/usr/lib/x86_64-linux-gnu/libasan.so.4+0x5166d)
    #1 0x55ff72e70067 in strcat /usr/include/x86_64-linux-gnu/bits/string_fortified.h:128
    #2 0x55ff72e70067 in htmlescape ../../programs/escape.c:48
    #3 0x55ff72e6dbb5 in output_TEXT ../../programs/dwg2SVG.c:113
"""


def test_libasan_module_offset_is_skipped_to_app_frame() -> None:
    assert _crash_signature(_LIBREDWG_WITH_LIBASAN_OFFSET) == (
        "heap-buffer-overflow",
        "read",
        "htmlescape",
    )


def test_confirmatory_cohort_19_fixtures_have_complete_signatures() -> None:
    """Gate: zero incomplete crash signatures across the confirmatory roster."""
    root = Path(__file__).resolve().parents[4]
    dataset = yaml.safe_load(
        (root / "experiments/b4-boss-manager-worker/confirmatory/dataset.yaml").read_text(
            encoding="utf-8"
        )
    )
    incomplete: list[str] = []
    for name in dataset["default_cves"]:
        report = json.loads(
            (root / f"plugins/security/tests/fixtures/{name}.json").read_text(
                encoding="utf-8"
            )
        )["sanitizer_report"]
        sig = crash_signature(report)
        if not (sig.complete and sig.access_kind is not None):
            incomplete.append(
                f"{name}: class={sig.sanitizer_class!r} access={sig.access_kind!r} "
                f"frame={sig.top_application_frame!r}"
            )
    assert incomplete == [], "incomplete signatures:\n" + "\n".join(incomplete)
