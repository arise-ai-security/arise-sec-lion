"""Design Structure Matrix (DSM) construction + dependency-free SVG heatmaps.

A DSM places interacting units on both axes; cell (i, j) is the interaction
weight between them. Ordering units by module makes cohesion show up as dense
block-diagonals and coupling as off-block cells. Two views:

  * node x node for one run — the "hero" figure (block-diagonal = modules);
  * module x module aggregated across runs — the summary figure.

SVG is emitted by hand (no matplotlib): a grid of colored ``<rect>`` cells with
axis labels and module-block separators.
"""

from __future__ import annotations

import collections
from typing import TYPE_CHECKING

from experiments.shared.scripts.analysis.modularity.claims import interaction_weights

if TYPE_CHECKING:
    from experiments.shared.scripts.analysis.modularity.normalize import RunModel

_MODULE_ORDER = ["builder", "exploiter", "fixer", "reporter", "boss"]


def module_interaction_matrix(rm: RunModel) -> dict[tuple[str, str], float]:
    """Undirected interaction weight aggregated per module pair (incl. boss)."""
    matrix: dict[tuple[str, str], float] = collections.Counter()
    for (u, v), w in interaction_weights(rm).items():
        key = tuple(sorted((rm.module_of(u), rm.module_of(v))))
        matrix[key] += w
    return dict(matrix)


def module_dataflow_matrix(rm: RunModel) -> dict[tuple[str, str], int]:
    """Directed producer->consumer counts per module pair (from dataflow)."""
    matrix: dict[tuple[str, str], int] = collections.Counter()
    for flow in rm.dataflows:
        matrix[(flow.writer_module, flow.reader_module)] += 1
    return dict(matrix)


def _module_rank(module: str) -> tuple[int, str]:
    return (_MODULE_ORDER.index(module) if module in _MODULE_ORDER else len(_MODULE_ORDER), module)


def node_dsm(rm: RunModel) -> tuple[list[str], list[str], list[list[float]], list[int]]:
    """Node x node interaction matrix, nodes grouped by module.

    Returns (ordered node ids, per-node module label, symmetric matrix, block
    sizes per module group in display order).
    """
    weights = interaction_weights(rm)
    by_module: dict[str, list[str]] = collections.defaultdict(list)
    for node in rm.table.nodes:
        by_module[rm.module_of(node)].append(node)

    ordered: list[str] = []
    labels: list[str] = []
    blocks: list[int] = []
    for module in sorted(by_module, key=_module_rank):
        members = sorted(
            by_module[module],
            key=lambda n: (rm.table.nodes[n].depth, rm.table.nodes[n].sibling_index or 0, n),
        )
        for node in members:
            ordered.append(node)
            labels.append(module)
        blocks.append(len(members))

    index = {n: i for i, n in enumerate(ordered)}
    size = len(ordered)
    matrix = [[0.0] * size for _ in range(size)]
    for (u, v), w in weights.items():
        if u in index and v in index:
            matrix[index[u]][index[v]] = w
            matrix[index[v]][index[u]] = w
    return ordered, labels, matrix, blocks


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _color(t: float) -> str:
    """Light->dark blue ramp for t in [0, 1] (ColorBrewer Blues endpoints)."""
    t = max(0.0, min(1.0, t))
    lo, hi = (247, 251, 255), (8, 48, 107)
    return "#%02x%02x%02x" % tuple(round(lo[k] + (hi[k] - lo[k]) * t) for k in range(3))


def _fmt_val(value: float) -> str:
    return str(int(value)) if abs(value - round(value)) < 1e-9 else f"{value:.1f}"


def render_heatmap_svg(matrix: list[list[float]], row_labels: list[str], col_labels: list[str],
                       *, title: str, annotate: bool = False,
                       block_boundaries: list[int] | None = None, cell: int = 48) -> str:
    rows = len(matrix)
    cols = len(matrix[0]) if rows else 0
    maxval = max((max(r) for r in matrix if r), default=0.0) or 1.0
    left, top = 150, 104
    width, height = left + cols * cell + 30, top + rows * cell + 40
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        'font-family="ui-monospace,Menlo,monospace" font-size="11">',
        f'<rect width="{width}" height="{height}" fill="white"/>',
        f'<text x="14" y="26" font-size="15" font-weight="bold">{_esc(title)}</text>',
        f'<text x="14" y="44" font-size="10" fill="#666">darker = more interaction '
        f'(max {_fmt_val(maxval)}); black lines = module boundaries</text>',
    ]
    for j, label in enumerate(col_labels):
        cx = left + j * cell + cell / 2
        out.append(f'<text x="{cx}" y="{top - 6}" transform="rotate(-45 {cx} {top - 6})">{_esc(label)}</text>')
    for i, row in enumerate(matrix):
        y = top + i * cell
        out.append(f'<text x="{left - 8}" y="{y + cell / 2 + 4}" text-anchor="end">{_esc(row_labels[i])}</text>')
        for j, value in enumerate(row):
            x = left + j * cell
            shade = value / maxval
            out.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" '
                       f'fill="{_color(shade)}" stroke="#dde" stroke-width="0.5"/>')
            if annotate and value:
                color = "#fff" if shade > 0.5 else "#123"
                out.append(f'<text x="{x + cell / 2}" y="{y + cell / 2 + 4}" text-anchor="middle" '
                           f'fill="{color}">{_fmt_val(value)}</text>')
    if block_boundaries:
        cumulative = 0
        for size in block_boundaries[:-1]:
            cumulative += size
            gx, gy = left + cumulative * cell, top + cumulative * cell
            out.append(f'<line x1="{gx}" y1="{top}" x2="{gx}" y2="{top + rows * cell}" stroke="#111" stroke-width="1.5"/>')
            out.append(f'<line x1="{left}" y1="{gy}" x2="{left + cols * cell}" y2="{gy}" stroke="#111" stroke-width="1.5"/>')
    out.append("</svg>")
    return "\n".join(out)


def module_matrix_to_grid(pair_weights: dict[tuple[str, str], float], modules: list[str],
                          *, symmetric: bool) -> list[list[float]]:
    """Lay a {(a,b): w} map onto a dense modules x modules grid."""
    grid = [[0.0] * len(modules) for _ in modules]
    idx = {m: i for i, m in enumerate(modules)}
    for (a, b), w in pair_weights.items():
        if a not in idx or b not in idx:
            continue
        grid[idx[a]][idx[b]] += w
        if symmetric and a != b:
            grid[idx[b]][idx[a]] += w
    return grid
