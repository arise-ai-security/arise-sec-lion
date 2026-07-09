"""Criteria evaluation: deliverable/artifact metrics (#5, #6), the contract-only
per-run verdict, and the LLM-judge prompt builders.

This package was split from a single module by concern; the same dotted path
(``experiments.shared.evaluation.criteria``) still re-exports every name importers
depend on:

- :mod:`metrics` — artifacts per subtree (#5) and deliverable success (#6).
- :mod:`verdict` — :func:`evaluate_run`, the single per-run SEC-bench verdict.
- :mod:`judge_prompts` — the four (built, never invoked) LLM-judge prompts.

The package imports ZERO ``plugins/security`` code (composition boundary).
"""

from __future__ import annotations

from .judge_prompts import (
    _has_secb_launch,
    build_binary_genuine_prompt,
    build_cve_reproduced_prompt,
    build_execution_provenance_prompt,
    build_patch_root_cause_prompt,
)
from .metrics import (
    _HIERARCHICAL_ROLE_SPECIFIC_FIXER,
    _MAY_BE_EMPTY,
    _REQUIRED_FILES,
    _ROLE_DEPENDS_ON,
    _ROLE_PHASE,
    _VALIDATION_REQUIRED,
    ALL_KEY_FILES,
    KEY_FILES,
    artifacts_by_bef,
    key_file_exists,
    success_criteria_by_bef,
)
from .verdict import (
    PhaseVerdict,
    _crash_signature,
    _crash_signature_matches,
    built_phase,
    declared_path_exists,
    evaluate_run,
    exploited_phase,
    fixed_phase,
    literal,
    nonvacuous,
    present,
    references,
    reported_phase,
    runtime_seal_ok,
)


__all__ = [
    "ALL_KEY_FILES",
    "KEY_FILES",
    "PhaseVerdict",
    "artifacts_by_bef",
    "build_binary_genuine_prompt",
    "build_cve_reproduced_prompt",
    "build_execution_provenance_prompt",
    "build_patch_root_cause_prompt",
    "built_phase",
    "declared_path_exists",
    "evaluate_run",
    "exploited_phase",
    "fixed_phase",
    "key_file_exists",
    "literal",
    "nonvacuous",
    "present",
    "references",
    "reported_phase",
    "runtime_seal_ok",
    "success_criteria_by_bef",
]
