"""Subtree-modularization analysis for the agentic-tree system.

Verifies, from the canonical event store, that a run's prescribed modules
(Builder / Exploiter / Fixer / Reporter subtrees) execute with high
intra-module cohesion and low inter-module coupling — no duplicated tasks,
no file-write contention, minimal cross-module global-context access.

Design + plan: ``experiments/b1-batch-autogen/MODULARIZATION_ANALYSIS_DESIGN.md``
and ``…/MODULARIZATION_ANALYSIS_PLAN.md``.
"""
