"""Security domain plugin exports."""

from plugins.security.benchmark_result import BenchmarkResult, StageResult
from plugins.security.cve_inference import CVEInferenceError, CVEInstanceInferenceService
from plugins.security.cve_instance import CVEInstance
from plugins.security.image_resolver import resolve_secbench_image
from plugins.security.null_prompt_strategy import NullPromptStrategy
from plugins.security.plugin import SecurityDomainPlugin
from plugins.security.prompt_strategy import SecBenchPromptStrategy, detect_benchmark_branch


__all__ = [
    "BenchmarkResult",
    "CVEInferenceError",
    "CVEInstance",
    "CVEInstanceInferenceService",
    "NullPromptStrategy",
    "SecBenchPromptStrategy",
    "SecurityDomainPlugin",
    "StageResult",
    "detect_benchmark_branch",
    "resolve_secbench_image",
]
