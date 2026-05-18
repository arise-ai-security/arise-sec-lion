"""Forbidden-web detection over ``tool_use`` events (design doc §9).

Captures both direct invocations of ``WebFetch`` / ``WebSearch`` and
indirect attempts via Bash commands matching a curated web-egress
regex. The raw command text is preserved in ``tool_or_cmd`` and
``evidence_snippet`` (capped at ``_EVIDENCE_CAP`` characters).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final

from experiments.shared.scripts.analysis.models import WebViolation

from .prefixes import recover_tool_name


if TYPE_CHECKING:
    from collections.abc import Sequence

    from experiments.shared.scripts.db.models import EventRow


BASH_WEB_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(curl|wget|nc(?:at)?\s+-|"
    r"python3?\s+-c\s+['\"].*?(?:requests|urllib|httpx)|"
    r"socat\s+|openssl\s+s_client|ftp\s+|sftp\s+)"
    r"|https?://",
    re.IGNORECASE,
)
_DIRECT_WEB_TOOL_NAMES: Final[frozenset[str]] = frozenset({"WebFetch", "WebSearch"})
_INPUT_PREFIX = "\nInput: "
_EVIDENCE_CAP = 500


def detect_violations(events: Sequence[EventRow]) -> list[WebViolation]:
    """Scan ``events`` for direct and indirect forbidden-web attempts."""
    out: list[WebViolation] = []
    for e in events:
        if e.event_type != "ThoughtCaptured":
            continue
        payload = e.payload
        if payload.get("output_type") != "tool_use":
            continue
        content = payload.get("content") or ""
        tool_name = recover_tool_name(content, payload.get("tool_name"))
        if tool_name in _DIRECT_WEB_TOOL_NAMES:
            out.append(
                WebViolation(
                    via="direct_tool",
                    tool_or_cmd=tool_name,
                    evidence_snippet=content[:_EVIDENCE_CAP],
                    aggregate_id=e.aggregate_id,
                    sequence_number=e.sequence_number,
                )
            )
            continue
        if tool_name == "Bash":
            cmd = _extract_bash_command(content)
            if cmd and BASH_WEB_RE.search(cmd):
                out.append(
                    WebViolation(
                        via="bash_command",
                        tool_or_cmd=cmd[:_EVIDENCE_CAP],
                        evidence_snippet=cmd[:_EVIDENCE_CAP],
                        aggregate_id=e.aggregate_id,
                        sequence_number=e.sequence_number,
                    )
                )
    return out


def _extract_bash_command(content: str) -> str | None:
    """Return the ``command`` string from a Bash ``tool_use`` content blob."""
    idx = content.find(_INPUT_PREFIX)
    if idx < 0:
        return None
    try:
        obj = json.loads(content[idx + len(_INPUT_PREFIX) :])
    except (json.JSONDecodeError, TypeError):
        return None
    cmd = obj.get("command") if isinstance(obj, dict) else None
    return cmd if isinstance(cmd, str) else None
