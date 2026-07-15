"""Keep the evaluation artifact mirror aligned with the security plugin contract."""

from experiments.shared.evaluation.criteria.metrics import (
    _MAY_BE_EMPTY,
    _REQUIRED_FILES,
    _VALIDATION_REQUIRED,
)
from plugins.security.deliverables import MAY_BE_EMPTY, REQUIRED_FILES, VALIDATION_REQUIRED


def test_evaluation_artifact_contract_matches_security_plugin() -> None:
    # Given: the runtime contract and the deliberately decoupled evaluation mirror.
    runtime_required = {phase: tuple(paths) for phase, paths in REQUIRED_FILES.items()}
    runtime_validation = {
        phase: tuple(paths) for phase, paths in VALIDATION_REQUIRED.items()
    }

    # Then: both layers require the same artifacts without a runtime cross-boundary import.
    assert _REQUIRED_FILES == runtime_required
    assert _VALIDATION_REQUIRED == runtime_validation
    assert _MAY_BE_EMPTY == MAY_BE_EMPTY
