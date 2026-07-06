"""Verification pipeline for completed worker output."""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from core.application.services.orchestration.truncation import head_tail
from core.domain.services import strip_markdown_code_block
from core.domain.services.config_resolver import ConfigResolver
from core.domain.services.context_update_parser import parse_context_update


if TYPE_CHECKING:
    from core.domain.aggregates.agent_session import AgentSession
    from core.ports.runtime_ports import FormatRepairerPort, LLMPort

logger = logging.getLogger(__name__)


class JudgeResponse(BaseModel):
    """Schema the judge LLM is expected to return.

    Defined as a Pydantic model so the format-repairer prompt is
    auto-generated from this single source of truth: adding/renaming a
    required field here propagates into the repair prompt next call.
    """

    score: int = Field(ge=0, le=100)
    feedback: str = ""


@lru_cache(maxsize=1)
def build_judge_schema_hint() -> str:
    schema = json.dumps(
        JudgeResponse.model_json_schema(), indent=2, sort_keys=True,
    )
    return f"""\
A JSON object with the worker-evaluation result. Auto-generated schema:

{schema}

In raw form:
  {{"score": <int 0..100>, "feedback": "<one-paragraph feedback>"}}

Preserve the score and feedback values from the malformed input
verbatim — do not re-interpret or rescale."""


JUDGE_SCHEMA_HINT: str = build_judge_schema_hint()


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

    _JUDGE_PASS_THRESHOLD = 60

    @staticmethod
    def _parse_judge_payload(raw: str) -> dict[str, Any]:
        """Parse a judge response into a dict, raising on failure.

        Handles markdown fences, <think> tags, and trailing content via
        the standard ``strip_markdown_code_block`` + ``raw_decode`` path.
        Raises ``json.JSONDecodeError`` or ``ValueError`` on failure.
        """
        clean = strip_markdown_code_block(raw)
        # raw_decode tolerates trailing prose/JSON that some Qwen responses
        # append after the primary object. No-op for GPT/Claude.
        data, _ = json.JSONDecoder().raw_decode(clean.lstrip())
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data).__name__}")
        return data

    def __init__(
        self,
        llm_port: "LLMPort",
        *,
        skip_judge: bool = False,
        format_repairer: "FormatRepairerPort | None" = None,
    ) -> None:
        self._llm_port = llm_port
        self._skip_judge = skip_judge
        self._format_repairer = format_repairer
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

        judge_passed, feedback, score = await self._verify_with_judge(
            result=result,
            success_criteria=agent.success_criteria,
            config=agent.config,
            report_context=report_context,
        )
        if judge_passed:
            agent.mark_verification_passed(feedback, score=score)
            return

        if not judge_passed:
            agent.mark_verification_failed(
                failed_stage="judge",
                feedback=feedback,
                stages_passed=stages_passed,
                score=score,
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
    ) -> tuple[bool, str, int]:
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
            "However, if the success criteria explicitly require a specific "
            "deliverable FILE to exist (e.g., '/testcase/root_cause_analysis.txt "
            "exists'), then the worker MUST have created that file. Check the "
            "work output for evidence of file creation (file editor usage, "
            "'cat /testcase/<file>' output, or 'ls /testcase/' listing). "
            "A worker that performed analysis but did NOT write the required "
            "deliverable file should FAIL — the file is the deliverable, not "
            "the terminal exploration.\n\n"
            "ARTIFACT NAMING: If the success criteria mention a specific filename "
            "(e.g., 'leak_notes.txt') but the worker produced equivalent content "
            "under a different name (e.g., 'static_analysis.md', 'root_cause.md'), "
            "that is a PASS. Judge the quality and completeness of the work, not "
            "whether the exact filename matches. The substance matters, not the label.\n\n"
            "ALSO VALID: If the worker found that a previous sibling worker "
            "already produced the required deliverables, and the worker validated "
            "those existing artifacts (read and confirmed their quality), that "
            "counts as a PASS. Avoiding redundant work is efficient, not lazy.\n\n"
            "BUILD TOOL TOLERANCE: If the success criteria mention a specific build "
            "tool (e.g., Ninja, CMake, Meson, Rake) but the worker used a different "
            "tool that successfully built the project, that is a PASS. The success "
            "criteria may contain incorrect assumptions about the project's build "
            "system. Judge by whether the build SUCCEEDED, not by which tool was used.\n\n"
            f"## Success Criteria\n{success_criteria}\n\n"
            f"{report_section}"
            f"## Work Output (command log, may be truncated)\n"
            f"{head_tail(_strip_ansi(result), 30000)}\n\n"
            "YOUR RESPONSE MUST BE EXACTLY ONE LINE OF VALID JSON, nothing else. "
            "No markdown, no explanation before or after, just the JSON object:\n"
            '{"score": 75, "feedback": "explanation of what was accomplished and any gaps"}\n'
            "Where score is an integer from 0 (nothing done) to 100 (fully complete). "
            "Scoring guide: 90-100 = fully met criteria, 60-89 = substantially met "
            "with minor gaps, 30-59 = partial progress, 0-29 = little to no progress."
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
                logger.warning(
                    "Judge returned empty response (model=%s), retrying once",
                    response.model,
                )
                # Retry once with higher token limit
                retry_config = dict(config_dict)
                retry_config["max_tokens"] = 1000
                retry_response = await self._llm_port.query_with_usage(prompt, retry_config)
                raw = retry_response.content
                if not raw or not raw.strip():
                    logger.warning("Judge retry also empty — failing verification")
                    return False, "Judge could not evaluate (empty response after retry)", 0

            try:
                data = self._parse_judge_payload(raw)
            except (json.JSONDecodeError, ValueError) as standard_err:
                if self._format_repairer is None:
                    raise
                logger.warning(
                    "Judge JSON parse failed (%s); invoking format repairer",
                    standard_err,
                )
                # Repairer failure (any exception, including network/auth)
                # must NOT mask the standard parse error or skip the
                # downstream regex fallback. Treat any repairer problem
                # as "repairer unavailable".
                try:
                    repaired = await self._format_repairer.repair(
                        raw, JUDGE_SCHEMA_HINT,
                    )
                except Exception as repair_exc:  # noqa: BLE001 — auxiliary
                    logger.warning(
                        "Judge format repairer raised (%s); falling back "
                        "to regex extraction",
                        repair_exc,
                    )
                    raise standard_err
                if not repaired or repaired == raw:
                    raise standard_err
                data = self._parse_judge_payload(repaired)

            score = int(data.get("score", 0))
            feedback = data.get("feedback", "")
            passed = score >= self._JUDGE_PASS_THRESHOLD
            return passed, feedback, min(100, max(0, score))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            logger.warning(
                "Judge JSON parse failed (%s), trying regex fallback. Raw: %.500s",
                e, raw,
            )
            feedback_match = re.search(r'"feedback"\s*:\s*"([^"]*)"', raw)
            score_match = re.search(r'"score"\s*:\s*(\d+)', raw)
            if score_match:
                score = min(100, max(0, int(score_match.group(1))))
                feedback = feedback_match.group(1) if feedback_match else ""
                passed = score >= self._JUDGE_PASS_THRESHOLD
                fallback_msg = f"Judge score {score} (extracted)"
                return passed, feedback or fallback_msg, score
            logger.warning(
                "Regex fallback also failed, failing verification. Raw length=%d",
                len(raw),
            )
            return (
                False,
                "Judge response could not be parsed — verification failed",
                0,
            )
