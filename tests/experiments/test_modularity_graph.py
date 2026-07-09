"""Known-answer tests for the pure graph math (Q, communities, NMI, ARI)."""

from __future__ import annotations

from experiments.shared.scripts.analysis.modularity.graph import (
    adjusted_rand_index,
    greedy_communities,
    modularity,
    normalized_mutual_info,
)


def test_modularity_two_disjoint_edges_is_half():
    # Given: two separate edges, each its own community
    nodes = ["a", "b", "c", "d"]
    weights = {("a", "b"): 1.0, ("c", "d"): 1.0}
    part = {"a": 0, "b": 0, "c": 1, "d": 1}
    # When/Then: Q = 2 * (1/2 - (2/4)^2) = 0.5
    assert abs(modularity(nodes, weights, part) - 0.5) < 1e-9


def test_modularity_single_community_is_zero():
    # Given: everything lumped into one community
    nodes = ["a", "b", "c", "d"]
    weights = {("a", "b"): 1.0, ("c", "d"): 1.0}
    # When/Then: Q = m/m - (2m/2m)^2 = 0
    assert abs(modularity(nodes, weights, {n: 0 for n in nodes})) < 1e-9


def test_modularity_empty_graph_is_zero():
    assert modularity(["a"], {}, {"a": 0}) == 0.0


def test_greedy_recovers_two_triangles():
    # Given: two triangles joined by a single bridge edge
    nodes = list("abcdef")
    weights = {("a", "b"): 1.0, ("a", "c"): 1.0, ("b", "c"): 1.0,
               ("d", "e"): 1.0, ("d", "f"): 1.0, ("e", "f"): 1.0,
               ("c", "d"): 1.0}
    # When: communities are detected
    comm = greedy_communities(nodes, weights)
    # Then: exactly two communities, matching the triangles
    assert len({comm[n] for n in nodes}) == 2
    assert comm["a"] == comm["b"] == comm["c"]
    assert comm["d"] == comm["e"] == comm["f"]
    assert comm["a"] != comm["d"]


def test_nmi_identical_is_one():
    a = {1: 0, 2: 0, 3: 1, 4: 1}
    assert abs(normalized_mutual_info(a, a) - 1.0) < 1e-9


def test_nmi_relabeled_identical_is_one():
    # Same grouping, different labels -> still 1
    a = {1: 0, 2: 0, 3: 1, 4: 1}
    b = {1: 9, 2: 9, 3: 5, 4: 5}
    assert abs(normalized_mutual_info(a, b) - 1.0) < 1e-9


def test_nmi_against_constant_partition_is_zero():
    a = {1: 0, 2: 0, 3: 1, 4: 1}
    b = {1: 0, 2: 0, 3: 0, 4: 0}
    assert normalized_mutual_info(a, b) == 0.0


def test_ari_identical_is_one():
    a = {1: 0, 2: 0, 3: 1, 4: 1}
    assert abs(adjusted_rand_index(a, a) - 1.0) < 1e-9


def test_ari_orthogonal_split_is_negative_half():
    # Classic n=4 case: {12}{34} vs {13}{24} -> ARI = -0.5
    a = {1: 0, 2: 0, 3: 1, 4: 1}
    b = {1: 0, 2: 1, 3: 0, 4: 1}
    assert abs(adjusted_rand_index(a, b) - (-0.5)) < 1e-9
