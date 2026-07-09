"""Cross-cutting helpers shared by the criteria concern modules.

``_read_text_safe`` lives here (not in :mod:`verdict`) because both the verdict
gates and the judge-prompt builders read it, and :mod:`verdict` imports
:mod:`judge_prompts` — keeping it in either would create an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
