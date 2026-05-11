"""Regression tests for plot_success.py SVG rendering.

Audit N-13: cell labels must be XML-escaped before being inserted into the
SVG output. Pre-fix the raw label was concatenated into a `<text>` body
verbatim — practical risk was zero since cell slugs are `A1`-shape, but
the code path was XML-injection-shaped.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path


logger = logging.getLogger(__name__)


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "plot_success.py"
_SPEC = importlib.util.spec_from_file_location("plot_success_test_module", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_PLOT_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PLOT_MODULE)


def test_render_svg_escapes_special_chars_in_cell_label() -> None:
    # Given: a synthetic row where the cell field contains XML metacharacters.
    rows = [{"cell": "<script>", "runs": "3"}]

    # When
    svg = _PLOT_MODULE._render_svg(rows).decode("utf-8")

    # Then: the raw label is not in the SVG; the escaped form is.
    assert "<script>" not in svg.split("</svg>")[0].replace("<svg ", "<SVG-OPEN ")
    assert "&lt;script&gt;" in svg


def test_render_svg_preserves_normal_cell_label() -> None:
    # Given: a normal cell slug
    rows = [{"cell": "A1", "runs": "5"}]

    # When
    svg = _PLOT_MODULE._render_svg(rows).decode("utf-8")

    # Then: it appears verbatim
    assert "A1" in svg
