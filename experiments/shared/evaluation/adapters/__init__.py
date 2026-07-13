"""Adapters for external reference evaluators."""

from .secbench import (
    SecBenchArtifactAdapter,
    SecBenchEvaluatorAdapter,
)

__all__ = ["SecBenchArtifactAdapter", "SecBenchEvaluatorAdapter"]
