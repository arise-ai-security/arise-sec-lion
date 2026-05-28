"""Security domain plugin exports."""

from plugins.security.benchmark_result import BenchmarkResult, StageResult
from plugins.security.cve_inference import CVEInferenceError, CVEInstanceInferenceService
from plugins.security.cve_instance import CVEInstance
from plugins.security.image_resolver import resolve_secbench_image
from plugins.security.plugin import SecurityDomainPlugin
from plugins.security.prompt_strategy import SecBenchPromptStrategy


__all__ = [
    "BenchmarkResult",
    "CVEInferenceError",
    "CVEInstance",
    "CVEInstanceInferenceService",
    "SecBenchPromptStrategy",
    "SecurityDomainPlugin",
    "StageResult",
    "resolve_secbench_image",
]
