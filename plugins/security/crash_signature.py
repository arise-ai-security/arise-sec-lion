"""Sanitizer crash-signature extraction and matching for SEC-bench procedures.

A crash signature is the opaque triple ``(sanitizer_class, access_kind,
top_application_frame)`` — enough to decide whether two sanitizer aborts are the
same defect regardless of LLM/agent variance. This is CVE logic and belongs in
``plugins/``.

``experiments/shared/evaluation/criteria.py`` keeps its own copy by design (the
eval package deliberately does not import ``plugins/``, SYSTEM_REFERENCE §V.2).
``plugins/security/tests/test_procedures.py`` carries a drift test asserting both
implementations agree on a fixture set; keep this module byte-compatible with that
copy's ``_crash_signature``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_ASAN_CLASS_RX = re.compile(r"ERROR:\s*AddressSanitizer:\s*([A-Za-z0-9_-]+)")
_ASAN_ACCESS_RX = re.compile(r"\b(READ|WRITE)\s+of\s+size\s+\d+", re.IGNORECASE)
_ASAN_ACCESS_CAUSE_RX = re.compile(r"caused by a\s+(READ|WRITE)\s+memory access", re.IGNORECASE)
_ASAN_FRAME_RX = re.compile(r"#\d+\s+0x[0-9a-fA-F]+\s+in\s+(.+)$")
_FRAME_PATH_RX = re.compile(r"\s+(/\S+|\([^)]*\))\s*$")
_NONAPP_FUNC_RX = re.compile(
    r"^(?:__asan|__lsan|__interceptor|__sanitizer|__isoc99_|asan_|scanf_common"
    r"|printf_common|v?[fs]?scanf|v?[fs]?printf|mem(?:cpy|move|set|cmp)"
    r"|str(?:n?cpy|n?cat|len|n?cmp|dup)|malloc|calloc|realloc|free"
    r"|operator new|operator delete|_start|__libc_start_main)",
    re.IGNORECASE,
)
_NONAPP_PATH_RX = re.compile(
    r"compiler-rt|sanitizer_common|/asan/|libc-start|/glibc|llvm-project|/sysdeps/|interception",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CrashSignature:
    """The opaque ``(class, access, top-app-frame)`` key of a sanitizer abort."""

    sanitizer_class: str | None
    access_kind: str | None
    top_frame: str | None

    @property
    def crashed(self) -> bool:
        """Whether the text carried a sanitizer error class (i.e. a crash fired)."""
        return self.sanitizer_class is not None

    def as_tuple(self) -> tuple[str | None, str | None, str | None]:
        """The raw triple, matching ``criteria._crash_signature`` output for drift tests."""
        return (self.sanitizer_class, self.access_kind, self.top_frame)


def compute_crash_signature(text: str) -> CrashSignature:
    """Extract ``(sanitizer_class, access_kind, top_application_frame)`` from ASan output.

    The top application frame skips sanitizer / interceptor / libc frames (memcpy,
    scanf_common, ``__asan_*``, glibc, compiler-rt) so the key is the project's own
    crash site, not the shim it aborted inside. A no-crash text yields an all-``None``
    signature.
    """
    if not text:
        return CrashSignature(None, None, None)
    cls_m = _ASAN_CLASS_RX.search(text)
    cls = cls_m.group(1).lower() if cls_m else None
    acc_m = _ASAN_ACCESS_RX.search(text) or _ASAN_ACCESS_CAUSE_RX.search(text)
    acc = acc_m.group(1).lower() if acc_m else None
    top: str | None = None
    for line in text.splitlines():
        fm = _ASAN_FRAME_RX.search(line)
        if not fm:
            continue
        rest = fm.group(1).strip()
        pm = _FRAME_PATH_RX.search(rest)
        func = (rest[: pm.start()] if pm else rest).strip()
        path = pm.group(1) if pm else ""
        base = re.split(r"[(<]", func)[0].strip()
        if not base or _NONAPP_FUNC_RX.match(base) or _NONAPP_PATH_RX.search(path):
            continue
        top = base
        break
    return CrashSignature(cls, acc, top)


def signatures_match(observed: CrashSignature, golden: CrashSignature) -> bool:
    """True iff the observed abort is the same defect as the golden oracle's.

    Requires class + top frame on both sides and (when the golden states one) the
    access kind. Mirrors ``criteria._crash_signature_matches`` so the plugin-side
    verdict and the offline evaluation floor never disagree.
    """
    if not (golden.sanitizer_class and golden.top_frame):
        return False
    if not (observed.sanitizer_class and observed.top_frame):
        return False
    return (
        observed.sanitizer_class == golden.sanitizer_class
        and observed.top_frame == golden.top_frame
        and (golden.access_kind is None or observed.access_kind == golden.access_kind)
    )
