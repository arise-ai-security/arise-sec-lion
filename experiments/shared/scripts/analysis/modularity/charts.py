"""Dependency-free SVG charts (grouped bars with error bars, histogram).

Paper-oriented: clean axes, a legend, and optional error bars for CIs/IQRs.
No matplotlib — small hand-rolled SVG so figures regenerate anywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_PALETTE = ["#08519c", "#6baed6", "#fd8d3c", "#74c476", "#9e9ac8", "#d6604d"]


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ticks(ymax: float, n: int = 5) -> list[float]:
    return [ymax * k / n for k in range(n + 1)]


def grouped_bar_svg(groups: Sequence[str], series: dict[str, Sequence[float]], *, title: str,
                    ylabel: str = "", errors: dict[str, Sequence[float]] | None = None,
                    ymax: float | None = None, colors: Sequence[str] | None = None) -> str:
    colors = list(colors or _PALETTE)
    names = list(series)
    left, right, top, bottom = 72, 150, 54, 96
    width, height = 760, 430
    pw, ph = width - left - right, height - top - bottom
    all_vals = [v + (errors[n][i] if errors and n in errors else 0)
                for n in names for i, v in enumerate(series[n])]
    top_val = ymax if ymax is not None else (max(all_vals, default=1.0) or 1.0)
    y_of = lambda v: top + ph - (v / top_val) * ph

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        'font-family="ui-sans-serif,Helvetica,Arial,sans-serif" font-size="12">',
        f'<rect width="{width}" height="{height}" fill="white"/>',
        f'<text x="14" y="26" font-size="15" font-weight="bold">{_esc(title)}</text>',
    ]
    for tick in _ticks(top_val):
        y = y_of(tick)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + pw}" y2="{y:.1f}" stroke="#eee"/>')
        out.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#555">{tick:.2f}</text>')
    if ylabel:
        out.append(f'<text x="16" y="{top + ph / 2:.0f}" transform="rotate(-90 16 {top + ph / 2:.0f})" '
                   f'text-anchor="middle" fill="#333">{_esc(ylabel)}</text>')

    gw = pw / max(len(groups), 1)
    bw = gw * 0.8 / max(len(names), 1)
    for gi, group in enumerate(groups):
        gx = left + gi * gw + gw * 0.1
        for si, name in enumerate(names):
            val = series[name][gi]
            x = gx + si * bw
            y = y_of(val)
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{top + ph - y:.1f}" '
                       f'fill="{colors[si % len(colors)]}"/>')
            if errors and name in errors and errors[name][gi]:
                e = errors[name][gi]
                cx = x + bw / 2
                out.append(f'<line x1="{cx:.1f}" y1="{y_of(val + e):.1f}" x2="{cx:.1f}" '
                           f'y2="{y_of(max(val - e, 0)):.1f}" stroke="#333"/>')
                out.append(f'<line x1="{cx - 3:.1f}" y1="{y_of(val + e):.1f}" x2="{cx + 3:.1f}" '
                           f'y2="{y_of(val + e):.1f}" stroke="#333"/>')
        out.append(f'<text x="{left + gi * gw + gw / 2:.1f}" y="{top + ph + 18}" text-anchor="middle">'
                   f'{_esc(group)}</text>')
    out.append(f'<line x1="{left}" y1="{top + ph}" x2="{left + pw}" y2="{top + ph}" stroke="#333"/>')
    for si, name in enumerate(names):
        ly = top + 4 + si * 20
        out.append(f'<rect x="{left + pw + 16}" y="{ly}" width="12" height="12" fill="{colors[si % len(colors)]}"/>')
        out.append(f'<text x="{left + pw + 32}" y="{ly + 11}">{_esc(name)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def histogram_svg(values: Sequence[float], *, title: str, bins: int = 20, xlabel: str = "",
                  vline: float | None = None) -> str:
    left, right, top, bottom = 64, 30, 54, 80
    width, height = 760, 400
    pw, ph = width - left - right, height - top - bottom
    vals = [v for v in values if v is not None]
    if not vals:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"></svg>'
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1.0
    edges = [lo + (hi - lo) * k / bins for k in range(bins + 1)]
    counts = [0] * bins
    for v in vals:
        idx = min(int((v - lo) / (hi - lo) * bins), bins - 1)
        counts[idx] += 1
    cmax = max(counts) or 1
    x_of = lambda v: left + (v - lo) / (hi - lo) * pw
    y_of = lambda c: top + ph - (c / cmax) * ph

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        'font-family="ui-sans-serif,Helvetica,Arial,sans-serif" font-size="12">',
        f'<rect width="{width}" height="{height}" fill="white"/>',
        f'<text x="14" y="26" font-size="15" font-weight="bold">{_esc(title)}</text>',
    ]
    bw = pw / bins
    for i, c in enumerate(counts):
        x = left + i * bw
        y = y_of(c)
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw - 1:.1f}" height="{top + ph - y:.1f}" '
                   f'fill="#6baed6"/>')
    out.append(f'<line x1="{left}" y1="{top + ph}" x2="{left + pw}" y2="{top + ph}" stroke="#333"/>')
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        v = lo + (hi - lo) * frac
        out.append(f'<text x="{x_of(v):.1f}" y="{top + ph + 18}" text-anchor="middle" fill="#555">{v:.2f}</text>')
    if vline is not None and lo <= vline <= hi:
        out.append(f'<line x1="{x_of(vline):.1f}" y1="{top}" x2="{x_of(vline):.1f}" y2="{top + ph}" '
                   'stroke="#d6604d" stroke-width="2" stroke-dasharray="4 3"/>')
        out.append(f'<text x="{x_of(vline) + 4:.1f}" y="{top + 12}" fill="#d6604d">{_esc(xlabel or "")}</text>')
    elif xlabel:
        out.append(f'<text x="{left + pw / 2:.0f}" y="{height - 16}" text-anchor="middle" fill="#333">{_esc(xlabel)}</text>')
    out.append("</svg>")
    return "\n".join(out)
