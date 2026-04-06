"""Verification pipeline for completed worker output."""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.domain.services import strip_markdown_code_block
from core.domain.services.config_resolver import ConfigResolver
from core.domain.services.context_update_parser import parse_context_update


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


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\[\?[0-9]*[hlm]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from worker output."""
    return _ANSI_ESCAPE.sub("", text)


@dataclass(frozen=True)
class VerificationStage:
    """A deterministic verification stage."""

    name: str
    check: Callable[[str], bool]
    failure_feedback: str


class VerificationPipeline:
    """Run deterministic and judge-based verification for worker output."""

    def __init__(self, llm_port: "LLMPort", *, skip_judge: bool = False) -> None:
        self._llm_port = llm_port
        self._skip_judge = skip_judge
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

        if not agent.success_criteria or self._skip_judge:
            agent.mark_verification_passed(
                "Passed structural checks (no success criteria defined for judge evaluation)"
            )
            return

        report_context = self._build_report_context(agent)

        judge_passed, feedback = await self._verify_with_judge(
            result=result,
            success_criteria=agent.success_criteria,
            config=agent.config,
            report_context=report_context,
        )
        if judge_passed:
            agent.mark_verification_passed(feedback)
            return

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

    @staticmethod
    def _build_report_context(agent: "AgentSession") -> str:
        """Build structured report context from worker's result and context updates.

        Combines the agent's structured Report (task, artifacts, decisions) with
        any <context-update> blocks parsed from the raw result. This gives the
        verification judge structured evidence beyond the truncated raw output.
        """
        lines: list[str] = []

        # Structured report from agent
        report = agent.build_report()
        if report.task:
            lines.append(f"Task assigned: {report.task[:500]}")
        if report.artifacts:
            lines.append(f"Artifacts produced: {', '.join(report.artifacts)}")
        if report.decisions:
            lines.append(f"Decisions made: {', '.join(report.decisions)}")
        if report.execution_summary:
            lines.append(f"Execution summary: {report.execution_summary}")

        # Briefing context from parent
        if agent.briefing:
            if agent.briefing.subtask_justification:
                for k, v in agent.briefing.subtask_justification.items():
                    lines.append(f"Parent guidance [{k}]: {v[:200]}")

        # Parse structured context updates from worker output
        if agent.result:
            parsed = parse_context_update(agent.result)
            if parsed:
                for d in parsed.decisions:
                    lines.append(f"Decision [{d.key}]: {d.value} (rationale: {d.rationale})")
                for a in parsed.artifacts:
                    lines.append(f"Output [{a.key}]: {a.description}")

        return "\n".join(lines)

    async def _verify_with_judge(
        self,
        *,
        result: str,
        success_criteria: str,
        config: Any,
        report_context: str = "",
    ) -> tuple[bool, str]:
        """Stage 4: LLM judge evaluates output against success criteria."""
        report_section = ""
        if report_context:
            report_section = (
                f"## Worker Report (structured summary)\n{report_context}\n\n"
            )

        prompt = (
            "You are a quality judge. Evaluate whether the worker's output "
            "satisfies the success criteria. The worker executed inside a "
            "container and the raw output contains terminal observations "
            "which may be truncated. Focus on evidence of task completion "
            "rather than requiring every intermediate step to be visible.\n\n"
            "IMPORTANT: Judge based on the ACTUAL WORK DONE, not output format. "
            "If the worker accomplished the security analysis goal (found the bug, "
            "built the code, created the patch, etc.) but didn't produce a "
            "specific artifact file, that should still PASS.\n\n"
            "ARTIFACT NAMING: If the success criteria mention a specific filename "
            "(e.g., 'leak_notes.txt') but the worker produced equivalent content "
            "under a different name (e.g., 'static_analysis.md', 'root_cause.md'), "
            "that is a PASS. Judge the quality and completeness of the work, not "
            "whether the exact filename matches. The substance matters, not the label.\n\n"
            "ALSO VALID: If the worker found that a previous sibling worker "
            "already produced the required deliverables, and the worker validated "
            "those existing artifacts (read and confirmed their quality), that "
            "counts as a PASS. Avoiding redundant work is efficient, not lazy.\n\n"
            f"## Success Criteria\n{success_criteria}\n\n"
            f"{report_section}"
            f"## Work Output (command log, may be truncated)\n"
            f"{_head_tail(_strip_ansi(result), 30000)}\n\n"
            "YOUR RESPONSE MUST BE EXACTLY ONE LINE OF VALID JSON, nothing else. "
            "No markdown, no explanation before or after, just the JSON object:\n"
            '{"passed": true, "feedback": "explanation of where deliverables were found"}\n'
            "or\n"
            '{"passed": false, "feedback": "explanation of what is missing"}'
        )

        raw = ""
        try:
            llm_config = ConfigResolver.resolve(config, operation="complexity_evaluation")
            # Judge needs enough tokens for a JSON line — ensure at least 200
            config_dict = llm_config.model_dump()
            if config_dict.get("max_tokens", 0) < 200:
                config_dict["max_tokens"] = 200
            response = await self._llm_port.query_with_usage(prompt, config_dict)
            raw = response.content

            if not raw or not raw.strip():
                logger.warning("Judge returned empty response (model=%s), retrying once", response.model)
                # Retry once with higher token limit
                retry_config = dict(config_dict)
                retry_config["max_tokens"] = 1000
                retry_response = await self._llm_port.query_with_usage(prompt, retry_config)
                raw = retry_response.content
                if not raw or not raw.strip():
                    logger.warning("Judge retry also empty — failing verification")
                    return False, "Judge could not evaluate (empty response after retry)"

            clean = strip_markdown_code_block(raw)
            data = json.loads(clean)
            if not isinstance(data, dict):
                raise ValueError(f"Expected dict, got {type(data).__name__}")
            return bool(data.get("passed", False)), data.get("feedback", "")
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            logger.warning("Judge JSON parse failed (%s), trying regex fallback. Raw: %.500s", e, raw)
            # Regex fallback: extract passed/feedback from malformed JSON
            passed_match = re.search(r'"passed"\s*:\s*(true|false)', raw, re.IGNORECASE)
            feedback_match = re.search(r'"feedback"\s*:\s*"([^"]*)"', raw)
            if passed_match:
                passed = passed_match.group(1).lower() == "true"
                feedback = feedback_match.group(1) if feedback_match else ""
                return passed, feedback or ("Judge passed (extracted from malformed response)" if passed else "Judge failed (extracted from malformed response)")
            logger.warning("Regex fallback also failed, failing verification. Raw length=%d", len(raw))
            return False, "Judge response could not be parsed — verification failed"
