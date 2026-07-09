"""Pure graph math: weighted modularity Q, community detection, NMI, ARI.

Kept free of any project types so it is trivially testable and independently
verifiable. Graphs are tiny (one run = a few-dozen nodes), so community
detection is greedy agglomerative merging that re-evaluates the verified
:func:`modularity` directly — this avoids error-prone incremental-delta-Q
algebra at no meaningful cost.

Conventions: ``nodes`` is an iterable of node ids; ``weights`` is an
undirected weighted edge map keyed by a sorted ``(u, v)`` tuple (u < v, no
self-loops); ``partition`` maps each node to a community label.
"""

from __future__ import annotations

import collections
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Hashable, Iterable, Mapping

Node = "Hashable"


def degrees(weights: Mapping[tuple, float]) -> dict:
    """Weighted degree per node (each incident edge weight added once per end)."""
    deg: dict = collections.defaultdict(float)
    for (u, v), w in weights.items():
        deg[u] += w
        deg[v] += w
    return dict(deg)


def modularity(nodes: Iterable, weights: Mapping[tuple, float], partition: Mapping) -> float:
    """Newman weighted modularity Q = Σ_c [ L_c/m − (D_c/2m)² ].

    ``L_c`` = internal edge weight of community c; ``D_c`` = summed degree of
    its nodes; ``m`` = total edge weight. Range (−0.5, 1]; higher = stronger
    community structure than a degree-preserving random graph.
    """
    nodes = list(nodes)
    m = sum(weights.values())
    if m == 0:
        return 0.0
    deg = degrees(weights)
    internal: dict = collections.defaultdict(float)
    for (u, v), w in weights.items():
        if partition.get(u) == partition.get(v):
            internal[partition.get(u)] += w
    deg_c: dict = collections.defaultdict(float)
    for node in nodes:
        deg_c[partition.get(node)] += deg.get(node, 0.0)
    return sum(
        internal[c] / m - (deg_c[c] / (2 * m)) ** 2
        for c in {partition.get(n) for n in nodes}
    )


def greedy_communities(nodes: Iterable, weights: Mapping[tuple, float]) -> dict:
    """Agglomerative modularity maximization; returns ``node -> community int``.

    Each node starts in its own community; the pair of edge-connected
    communities whose merge yields the largest Q gain is merged, repeating
    until no merge improves Q. Deterministic (candidate pairs sorted; strict
    improvement threshold).
    """
    # Include any edge endpoints not explicitly listed so a stray edge can
    # never KeyError; callers normally pass a superset already.
    nodes = list(dict.fromkeys([*nodes, *(n for edge in weights for n in edge)]))
    part: dict = {n: i for i, n in enumerate(nodes)}

    def connected_pairs() -> list[tuple]:
        pairs = set()
        for (u, v) in weights:
            cu, cv = part[u], part[v]
            if cu != cv:
                pairs.add((cu, cv) if cu < cv else (cv, cu))
        return sorted(pairs)

    current = modularity(nodes, weights, part)
    improved = True
    while improved:
        improved = False
        best_pair = None
        best_q = current
        for ca, cb in connected_pairs():
            trial = {n: (ca if c == cb else c) for n, c in part.items()}
            q = modularity(nodes, weights, trial)
            # Strict improvement past a floating-point noise floor. For the
            # integer-weighted interaction graphs here, the smallest meaningful
            # Q gain is orders of magnitude above 1e-12, so this never skips a
            # real merge (only FP-noise "merges" worth ~1e-15).
            if q > best_q + 1e-12:
                best_q, best_pair = q, (ca, cb)
        if best_pair is not None:
            ca, cb = best_pair
            part = {n: (ca if c == cb else c) for n, c in part.items()}
            current, improved = best_q, True

    relabel = {c: i for i, c in enumerate(sorted(set(part.values())))}
    return {n: relabel[c] for n, c in part.items()}


def _entropy(counts: Iterable[int], n: int) -> float:
    return -sum((c / n) * math.log(c / n) for c in counts if c > 0)


def normalized_mutual_info(a: Mapping, b: Mapping) -> float:
    """NMI with arithmetic-mean normalization (sklearn default). 1 = identical."""
    nodes = set(a) & set(b)
    n = len(nodes)
    if n == 0:
        return 0.0
    ca = collections.Counter(a[x] for x in nodes)
    cb = collections.Counter(b[x] for x in nodes)
    joint = collections.Counter((a[x], b[x]) for x in nodes)
    mi = 0.0
    for (la, lb), cnt in joint.items():
        mi += (cnt / n) * math.log((cnt / n) / ((ca[la] / n) * (cb[lb] / n)))
    ha, hb = _entropy(ca.values(), n), _entropy(cb.values(), n)
    if ha == 0 and hb == 0:
        return 1.0  # both partitions trivial (one community) -> identical
    if ha + hb == 0:
        return 0.0
    return 2 * mi / (ha + hb)


def adjusted_rand_index(a: Mapping, b: Mapping) -> float:
    """Adjusted Rand Index. 1 = identical clustering, ~0 = random agreement."""
    nodes = set(a) & set(b)
    n = len(nodes)
    if n < 2:
        return 1.0
    cont = collections.Counter((a[x], b[x]) for x in nodes)
    ca = collections.Counter(a[x] for x in nodes)
    cb = collections.Counter(b[x] for x in nodes)

    def comb2(x: int) -> int:
        return x * (x - 1) // 2

    sum_ij = sum(comb2(v) for v in cont.values())
    sum_a = sum(comb2(v) for v in ca.values())
    sum_b = sum(comb2(v) for v in cb.values())
    total = comb2(n)
    expected = sum_a * sum_b / total if total else 0.0
    maximum = (sum_a + sum_b) / 2
    if maximum - expected == 0:
        return 1.0
    return (sum_ij - expected) / (maximum - expected)
