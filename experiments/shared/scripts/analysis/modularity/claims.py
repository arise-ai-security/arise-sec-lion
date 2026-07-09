"""Per-run claim metrics + permutation null.

All three claims reduce to comparing an observed separation statistic against a
**label-permutation null**: shuffle which module each (non-boss) node belongs to
— preserving module sizes — and recompute. If the observed coupling/overlap is
far below the null, the modular separation is real, not an artifact of how many
nodes each module has.

  * Claim 1 (coupling/cohesion): intra- vs inter-module interaction weight,
    modularity Q for the labeled partition, and whether unsupervised community
    detection recovers the labels (NMI/ARI). Inter-module *message* count is
    reported as a structural zero (tautology check).
  * Claim 2 (no duplicated work): cross-module Jaccard of recon-read file sets.
  * Claim 3 (no file contention): write-write overlap and producer->consumer
    dataflow, segmented by zone.
"""

from __future__ import annotations

import collections
import random
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from experiments.shared.scripts.analysis.modularity.graph import (
    adjusted_rand_index,
    greedy_communities,
    modularity,
    normalized_mutual_info,
)
from experiments.shared.scripts.analysis.modularity.modules import BOSS, UNKNOWN

if TYPE_CHECKING:
    from experiments.shared.scripts.analysis.modularity.normalize import RunModel

_READ_OPS = ("read", "search")
_WRITE_OPS = ("write", "edit")
_READ_CONF = ("high", "med")


def real_modules(rm: RunModel) -> list[str]:
    """The actual modules (excluding the boss root and unmapped nodes).

    For B1 this is exactly Builder/Exploiter/Fixer/Reporter; for the A1/A2
    handoff it is whatever the discovered-community assignment produced — so
    every metric below is strategy-agnostic.
    """
    return sorted(m for m in rm.assign.modules() if m not in (BOSS, UNKNOWN))


def message_weights(rm: RunModel) -> dict[tuple[str, str], float]:
    """Undirected weights from persisted messages only (tree edges)."""
    weights: dict[tuple[str, str], float] = collections.Counter()
    for msg in rm.messages:
        if msg.src != msg.dst:
            weights[tuple(sorted((msg.src, msg.dst)))] += 1
    return dict(weights)


def dataflow_weights(rm: RunModel) -> dict[tuple[str, str], float]:
    """Undirected weights from producer->consumer dataflow only (behavioral)."""
    weights: dict[tuple[str, str], float] = collections.Counter()
    for flow in rm.dataflows:
        if flow.writer != flow.reader:
            weights[tuple(sorted((flow.writer, flow.reader)))] += 1
    return dict(weights)


def interaction_weights(rm: RunModel) -> dict[tuple[str, str], float]:
    """Full interaction graph: messages + producer->consumer dataflow."""
    weights = collections.Counter(message_weights(rm))
    for edge, weight in dataflow_weights(rm).items():
        weights[edge] += weight
    return dict(weights)


def compute_coupling(rm: RunModel) -> dict:
    nodes = list(rm.table.nodes)
    weights = interaction_weights(rm)
    labeled = {n: rm.module_of(n) for n in nodes}

    intra = inter = boss = 0.0
    for (u, v), w in weights.items():
        mu, mv = labeled.get(u), labeled.get(v)
        if BOSS in (mu, mv):
            boss += w
        elif mu == mv:
            intra += w
        else:
            inter += w
    louvain = greedy_communities(nodes, weights)
    denom = intra + inter

    # Circularity disclosure (per verification): the interaction graph is mostly
    # tree-message weight, and modules ARE depth-1 subtrees, so recovering them
    # partly recovers the defining topology. Quantify it: message-weight share,
    # recovery on the dataflow-only (behavioral) graph, and a random-partition
    # baseline to show recovery beats chance even so.
    df_weights = dataflow_weights(rm)
    total_weight = sum(weights.values()) or 1.0
    louvain_df = greedy_communities(nodes, df_weights) if df_weights else {}
    rng = random.Random(99)
    real_nodes = [n for n in nodes if labeled.get(n) not in (BOSS, UNKNOWN)]
    real_labels = [labeled[n] for n in real_nodes]
    random_nmis = []
    for _ in range(20):
        shuffled = real_labels[:]
        rng.shuffle(shuffled)
        rand_part = dict(labeled)
        for node, label in zip(real_nodes, shuffled):
            rand_part[node] = label
        random_nmis.append(normalized_mutual_info(labeled, rand_part))

    return {
        "n_nodes": len(nodes),
        "n_modules": len({m for m in labeled.values() if m not in (BOSS, UNKNOWN)}),
        "intra_module_weight": intra,
        "inter_module_weight": inter,
        "boss_relay_weight": boss,
        "inter_module_fraction": (inter / denom) if denom else 0.0,
        "message_weight_fraction": sum(message_weights(rm).values()) / total_weight,
        "modularity_labeled": modularity(nodes, weights, labeled),
        "modularity_louvain": modularity(nodes, weights, louvain),
        "louvain_n_communities": len(set(louvain.values())),
        "recovery_nmi": normalized_mutual_info(labeled, louvain),
        "recovery_ari": adjusted_rand_index(labeled, louvain),
        "recovery_nmi_dataflow_only": normalized_mutual_info(labeled, louvain_df) if df_weights else None,
        "recovery_nmi_random_baseline": statistics.mean(random_nmis) if random_nmis else None,
        "inter_module_message_count": sum(1 for m in rm.messages if m.is_inter_module),
        "inter_module_dataflow_count": sum(1 for d in rm.dataflows if d.is_inter_module),
    }


def _module_path_sets(rm: RunModel, ops: tuple[str, ...], confs: tuple[str, ...],
                      real: set[str]) -> dict[str, set]:
    sets: dict[str, set] = collections.defaultdict(set)
    for touch in rm.touches:
        if touch.op in ops and touch.path and touch.confidence in confs:
            module = rm.module_of(touch.node_id)
            if module in real:
                sets[module].add(touch.path)
    return sets


def _mean_pairwise_jaccard(sets: dict[str, set], mods: list[str]) -> float:
    mods = [m for m in mods if sets.get(m)]
    if len(mods) < 2:
        return 0.0
    scores = []
    for i in range(len(mods)):
        for j in range(i + 1, len(mods)):
            a, b = sets[mods[i]], sets[mods[j]]
            union = len(a | b)
            scores.append(len(a & b) / union if union else 0.0)
    return statistics.mean(scores)


def compute_task_overlap(rm: RunModel) -> dict:
    """Claim 2: do modules duplicate recon/analysis work?"""
    real = real_modules(rm)
    read_sets = _module_path_sets(rm, _READ_OPS, _READ_CONF, set(real))
    structure = collections.Counter()
    for touch in rm.touches:
        if touch.op == "structure":
            module = rm.module_of(touch.node_id)
            structure[module] += 1
    return {
        "recon_read_mean_jaccard": _mean_pairwise_jaccard(read_sets, real),
        "recon_read_files_by_module": {m: len(s) for m, s in read_sets.items()},
        "structure_probes_by_module": dict(structure),
    }


def compute_file_overlap(rm: RunModel) -> dict:
    """Claim 3: write-write contention + producer/consumer dataflow, by zone."""
    real = set(real_modules(rm))
    write_by_zone: dict[str, dict[str, set]] = collections.defaultdict(lambda: collections.defaultdict(set))
    read_by_zone: dict[str, dict[str, set]] = collections.defaultdict(lambda: collections.defaultdict(set))
    for touch in rm.touches:
        module = rm.module_of(touch.node_id)
        if module not in real or not touch.path:
            continue
        zone = touch.zone or "other"
        if touch.op in _WRITE_OPS and touch.confidence == "high":
            write_by_zone[zone][touch.path].add(module)
        elif touch.op in _READ_OPS and touch.confidence in _READ_CONF:
            read_by_zone[zone][touch.path].add(module)

    ww = {z: sum(1 for mods in paths.values() if len(mods) >= 2) for z, paths in write_by_zone.items()}
    rr = {z: sum(1 for mods in paths.values() if len(mods) >= 2) for z, paths in read_by_zone.items()}
    ww_pairs: dict[str, int] = collections.Counter()
    for paths in write_by_zone.values():
        for mods in paths.values():
            ordered = sorted(mods)
            for i in range(len(ordered)):
                for j in range(i + 1, len(ordered)):
                    ww_pairs[f"{ordered[i]}+{ordered[j]}"] += 1
    pc_count: dict[str, int] = collections.Counter()
    pc_paths: dict[str, set] = collections.defaultdict(set)
    for flow in rm.dataflows:
        if flow.is_inter_module:
            zone = flow.zone or "other"
            pc_count[zone] += 1
            pc_paths[zone].add(flow.path)
    return {
        "write_write_overlap_by_zone": ww,
        "write_write_pairs": dict(ww_pairs),
        "read_overlap_by_zone": rr,
        "producer_consumer_edges_by_zone": dict(pc_count),
        "producer_consumer_paths_by_zone": {z: len(p) for z, p in pc_paths.items()},
    }


def compute_module_profiles(rm: RunModel) -> dict[str, dict]:
    """Per-module cohesion/coupling + Martin instability ``I = Ce/(Ca+Ce)``.

    Afferent ``Ca(m)`` = cross-module dataflows where m is the WRITER (others
    depend on m's output); efferent ``Ce(m)`` = where m is the READER (m
    depends on others). Builder (its ``/work/bin`` is read by everyone) → low I
    (stable); Reporter (reads all, read by none) → high I (unstable) — a
    Conway/mirroring check against the prescribed dependency DAG.
    """
    real = set(real_modules(rm))
    weights = interaction_weights(rm)
    intra: dict = collections.Counter()
    inter: dict = collections.Counter()
    for (u, v), w in weights.items():
        a, b = rm.module_of(u), rm.module_of(v)
        if a in real and b in real:
            if a == b:
                intra[a] += w
            else:
                inter[a] += w
                inter[b] += w
    ca: dict = collections.Counter()
    ce: dict = collections.Counter()
    for flow in rm.dataflows:
        if flow.is_inter_module:
            ca[flow.writer_module] += 1
            ce[flow.reader_module] += 1
    profiles: dict[str, dict] = {}
    for m in real:
        total = intra[m] + inter[m]
        degree = ca[m] + ce[m]
        profiles[m] = {
            "intra_weight": intra[m],
            "inter_weight": inter[m],
            "coupling_ratio": inter[m] / total if total else 0.0,
            "afferent_ca": ca[m],
            "efferent_ce": ce[m],
            "instability": ce[m] / degree if degree else None,
        }
    return profiles


def compute_global_context(rm: RunModel) -> dict:
    """Claim 1b: shared-store ("global context") writes, attributed by module."""
    real = set(real_modules(rm))
    by_module = collections.Counter()
    by_kind = collections.Counter()
    by_module_kind: dict = collections.defaultdict(collections.Counter)
    for write in rm.global_writes:
        by_kind[write.kind] += 1
        if write.author_module in real:
            by_module[write.author_module] += 1
            by_module_kind[write.author_module][write.kind] += 1
    return {
        "global_writes_total": len(rm.global_writes),
        "global_writes_by_kind": dict(by_kind),
        "global_writes_by_module": dict(by_module),
        "global_writes_by_module_kind": {m: dict(k) for m, k in by_module_kind.items()},
    }


# --------------------------------------------------------------------------- #
# Permutation null — precompute per-node sets so each shuffle is O(nodes).
# --------------------------------------------------------------------------- #


@dataclass
class _PermInputs:
    base: dict[str, str]
    movable: list[str]
    labels: list[str]
    node_reads: dict[str, set]
    node_writes: dict[str, set]
    dataflow_pairs: list[tuple[str, str]]
    real: frozenset[str]


def _perm_inputs(rm: RunModel) -> _PermInputs:
    base = rm.assign.assign_all()
    real = frozenset(real_modules(rm))
    movable = [n for n, m in base.items() if m in real]
    node_reads: dict[str, set] = collections.defaultdict(set)
    node_writes: dict[str, set] = collections.defaultdict(set)
    for touch in rm.touches:
        if not touch.path:
            continue
        if touch.op in _READ_OPS and touch.confidence in _READ_CONF:
            node_reads[touch.node_id].add(touch.path)
        elif touch.op in _WRITE_OPS and touch.confidence == "high":
            node_writes[touch.node_id].add(touch.path)
    pairs = [(d.writer, d.reader) for d in rm.dataflows if d.writer != d.reader]
    return _PermInputs(base, movable, [base[n] for n in movable], dict(node_reads),
                       dict(node_writes), pairs, real)


def _stat_dataflow_inter_fraction(pi: _PermInputs, mod: dict[str, str]) -> float:
    total = inter = 0
    for writer, reader in pi.dataflow_pairs:
        a, b = mod.get(writer), mod.get(reader)
        if a in pi.real and b in pi.real:
            total += 1
            if a != b:
                inter += 1
    return inter / total if total else 0.0


def _stat_read_jaccard(pi: _PermInputs, mod: dict[str, str]) -> float:
    sets: dict[str, set] = collections.defaultdict(set)
    for node, paths in pi.node_reads.items():
        m = mod.get(node)
        if m in pi.real:
            sets[m] |= paths
    return _mean_pairwise_jaccard(sets, sorted(pi.real))


def _stat_write_overlap(pi: _PermInputs, mod: dict[str, str]) -> float:
    by_path: dict[str, set] = collections.defaultdict(set)
    for node, paths in pi.node_writes.items():
        m = mod.get(node)
        if m in pi.real:
            for path in paths:
                by_path[path].add(m)
    return float(sum(1 for mods in by_path.values() if len(mods) >= 2))


_STATS: dict[str, Callable[[_PermInputs, dict], float]] = {
    "dataflow_inter_fraction": _stat_dataflow_inter_fraction,
    "recon_read_jaccard": _stat_read_jaccard,
    "write_write_overlap": _stat_write_overlap,
}


def permutation_nulls(rm: RunModel, n_perm: int = 1000, seed: int = 20260529) -> dict:
    """One-sided null (observed is LOW) for each separation statistic.

    Returns observed, null mean, p = P(perm <= observed), and z-score per stat.
    Deterministic given ``seed``.
    """
    pi = _perm_inputs(rm)
    rng = random.Random(seed)
    out: dict[str, dict] = {}
    if len(pi.movable) < 2:
        return {name: {"observed": fn(pi, pi.base), "null_mean": None, "p_value_low": None,
                       "z": None, "n_perm": 0} for name, fn in _STATS.items()}

    perms: list[dict[str, str]] = []
    for _ in range(n_perm):
        shuffled = pi.labels[:]
        rng.shuffle(shuffled)
        perm = dict(pi.base)
        for node, label in zip(pi.movable, shuffled):
            perm[node] = label
        perms.append(perm)

    for name, fn in _STATS.items():
        observed = fn(pi, pi.base)
        null_vals = [fn(pi, perm) for perm in perms]
        mean = statistics.mean(null_vals)
        sd = statistics.pstdev(null_vals)
        le = sum(1 for v in null_vals if v <= observed)
        out[name] = {
            "observed": observed,
            "null_mean": mean,
            "p_value_low": (le + 1) / (n_perm + 1),
            "z": ((observed - mean) / sd) if sd > 0 else 0.0,
            "n_perm": n_perm,
        }
    return out


def module_dataflow_counts(rm: RunModel) -> dict[str, int]:
    """Directed producer->consumer dataflow edge counts per module pair (``a->b``)."""
    counts: dict[str, int] = collections.Counter()
    for flow in rm.dataflows:
        counts[f"{flow.writer_module}->{flow.reader_module}"] += 1
    return dict(counts)


def compute_run_metrics(rm: RunModel, n_perm: int = 1000) -> dict:
    """All claim metrics for one run, JSON-serializable."""
    return {
        "run_id": rm.run_id,
        "task": rm.task,
        "exit_status": rm.exit_status,
        "coupling": compute_coupling(rm),
        "task_overlap": compute_task_overlap(rm),
        "file_overlap": compute_file_overlap(rm),
        "global_context": compute_global_context(rm),
        "module_profiles": compute_module_profiles(rm),
        "module_dataflow": module_dataflow_counts(rm),
        "permutation": permutation_nulls(rm, n_perm=n_perm),
    }
