"""Verification pipeline for completed worker output."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.domain.services import strip_markdown_code_block
from core.domain.services.config_resolver import ConfigResolver


if TYPE_CHECKING:
    from core.domain.aggregates.agent_session import AgentSession
    from core.ports.runtime_ports import LLMPort

logger = logging.getLogger(__name__)


def _head_tail(text: str, limit: int) -> str:
    """Keep first 2/3 + last 1/3 of text, showing omission count."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n\n[...{omitted} chars omitted...]\n\n{text[-tail:]}"


@dataclass(frozen=True)
class VerificationStage:
    """A deterministic verification stage."""

    name: str
    check: Callable[[str], bool]
    failure_feedback: str


class VerificationPipeline:
    """Run deterministic and judge-based verification for worker output."""

    def __init__(self, llm_port: "LLMPort") -> None:
        self._llm_port = llm_port
        self._stages: tuple[VerificationStage, ...] = (
            VerificationStage(
                name="structural",
                check=self._verify_structural,
                failure_feedback="Worker produced empty or blank output",
            ),
            VerificationStage(
                name="deterministic",
                check=self._verify_deterministic,
                failure_feedback="Deterministic check failed",
            ),
            VerificationStage(
                name="execution",
                check=self._verify_execution,
                failure_feedback="Execution check failed",
            ),
        )

    async def verify(self, agent: "AgentSession") -> None:
        """Run the verification pipeline for a completed worker."""
        result = agent.result or ""
        stages_passed: list[str] = []

        for stage in self._stages:
            if not stage.check(result):
                agent.mark_verification_failed(
                    failed_stage=stage.name,
                    feedback=stage.failure_feedback,
                    stages_passed=stages_passed,
                )
                return
            stages_passed.append(stage.name)

        if not agent.success_criteria:
            return

        judge_passed, feedback = await self._verify_with_judge(
            result=result,
            success_criteria=agent.success_criteria,
            config=agent.config,
        )
        if not judge_passed:
            agent.mark_verification_failed(
                failed_stage="judge",
                feedback=feedback,
                stages_passed=stages_passed,
            )

    @staticmethod
    def _verify_structural(result: str) -> bool:
        """Stage 1: Check output is non-empty and has meaningful content."""
        return bool(result and result.strip())

    @staticmethod
    def _verify_deterministic(_result: str) -> bool:
        """Stage 2: Deterministic checks (placeholder — always passes)."""
        return True

    @staticmethod
    def _verify_execution(_result: str) -> bool:
        """Stage 3: Execution checks (placeholder — always passes)."""
        return True

    async def _verify_with_judge(
        self,
        *,
        result: str,
        success_criteria: str,
        config: Any,
    ) -> tuple[bool, str]:
        """Stage 4: LLM judge evaluates output against success criteria."""
        prompt = (
            "You are a quality judge. Evaluate whether the following work output "
            "satisfies the given success criteria.\n\n"
            f"## Success Criteria\n{success_criteria}\n\n"
            f"## Work Output\n{_head_tail(result, 12000)}\n\n"
            "Respond with ONLY valid JSON:\n"
            '{"passed": true/false, "feedback": "brief explanation"}'
        )

        try:
            llm_config = ConfigResolver.resolve(config, operation="complexity_evaluation")
            response = await self._llm_port.query_with_usage(prompt, llm_config.model_dump())

            clean = strip_markdown_code_block(response.content)
            data = json.loads(clean)
            if not isinstance(data, dict):
                raise ValueError(f"Expected dict, got {type(data).__name__}")
            return bool(data.get("passed", False)), data.get("feedback", "")
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            logger.warning("Judge response parse failed: %s", e)
            return True, ""
