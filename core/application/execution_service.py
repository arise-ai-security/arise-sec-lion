"""Agent Execution Service - orchestrates agent lifecycle and execution."""

import asyncio
import contextlib
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


@dataclass
class BudgetConfig:
    """Configuration for cost budget tracking."""

    max_total_cost_usd: float = 100.0
    cost_warning_threshold: float = 0.8
    cost_tracking_enabled: bool = True

from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.domain.events import (
    ChildSpawned,
    DomainEvent,
    SubordinatesSpawned,
    VerifierSpawned,
)
from core.domain.exceptions import ConcurrencyError
from core.domain.model import AgentRole, AgentSession, AgentStatus
from core.domain.prompt_builder import PromptBuilder
from core.application.services.query_service import QueryService
from core.ports.context_dashboard_port import ContextDashboardPort
from core.ports.event_store_port import EventStorePort
from core.ports.llm_port import LLMPort
from core.ports.reward_mechanism_port import RewardMechanismPort
from core.ports.verification_heuristics_port import VerificationHeuristicsPort
from core.ports.worker_port import WorkerToolPort
from core.query.projections.hierarchy_collector import HierarchyCollector


# Default models for multi-model strategy (o3 family)
DEFAULT_SUBORDINATE_MODELS = [
    "o3",  # OpenAI o3 flagship
    "o3-mini",  # OpenAI o3 mini
    "o3",  # OpenAI o3 (redundant for reliability)
]


ProgressCallback = Callable[[DomainEvent, Any], None]
StatusCallback = Callable[[list[dict[str, Any]]], None]


class AgentExecutionService:
    """Application service for executing agent workflow steps.

    This service implements the orchestration logic (The Brain) that:
    - Loads aggregate state from event store
    - Dispatches to domain methods based on agent role/status
    - Persists uncommitted events with OCC
    - Handles concurrency conflicts with retry logic
    - Manages child agent lifecycle (creation and parent notifications)
    - Orchestrates the entire multi-agent system loop
    - Implements Task Assignment Workflow with verification and retry

    Task Assignment Workflow:
    1. Pop first task from queue
    2. Spawn 3 subordinates with different models (diversity for success)
    3. Wait for first success (terminate others) or all failures
    4. On success: trigger verification heuristics, recollect budget
    5. On failure: decide to retry (re-insert revised task) or end lifecycle
    6. If verification injected: add verification task to head of queue

    Attributes:
        event_store: Port for loading/saving domain events.
        llm_port: Port for LLM interactions (complexity evaluation, task analysis).
        worker_tool_port: Port for worker tool execution (e.g., Claude Code CLI).
        prompt_builder: Service for building hierarchical prompts.
        reward_mechanism: Port for budget recollection ratio calculations.
        verification_heuristics: Port for verification trigger decisions.
        model_config: Role-to-model mapping for agent configuration.
        subordinate_models: List of models to use for parallel subordinates.
        max_retries: Maximum number of OCC retry attempts (default: 3).
        poll_interval: Interval in seconds for polling active agents (default: 0.5).
    """

    def __init__(
        self,
        event_store: EventStorePort,
        llm_port: LLMPort,
        worker_tool_port: WorkerToolPort,
        prompt_builder: PromptBuilder | None = None,
        reward_mechanism: RewardMechanismPort | None = None,
        verification_heuristics: VerificationHeuristicsPort | None = None,
        context_dashboard: ContextDashboardPort | None = None,
        model_config: dict[str, str] | None = None,
        subordinate_models: list[str] | None = None,
        max_retries: int = 3,
        poll_interval: float = 0.5,
        output_directory: str | None = None,
        progress_callback: ProgressCallback | None = None,
        status_callback: StatusCallback | None = None,
        default_worker_tool: str = "claude_code",
        budget_config: BudgetConfig | None = None,
        system_limits: Any | None = None,
        worker_shortcut_probability: float = 0.3,
        budget_threshold_ratio: float = 0.02,
    ) -> None:
        """Initialize the execution service with infrastructure ports.

        Args:
            event_store: Event store implementation for persistence.
            llm_port: LLM adapter for agent reasoning.
            worker_tool_port: Worker tool adapter for task execution.
            prompt_builder: Prompt builder service. If None, creates default instance.
            model_config: Role-to-model mapping (keys: "boss", "manager", "worker", "pending").
                         If None, uses default o3-mini for all roles.
            max_retries: Maximum OCC retry attempts (default: 3).
            poll_interval: Interval in seconds for polling active agents (default: 0.5).
        """
        self.event_store = event_store
        self.llm_port = llm_port
        self.worker_tool_port = worker_tool_port
        self.prompt_builder = prompt_builder or PromptBuilder(default_tool=default_worker_tool)
        self.reward_mechanism = reward_mechanism
        self.verification_heuristics = verification_heuristics
        self.context_dashboard = context_dashboard
        self.max_retries = max_retries
        self.poll_interval = poll_interval
        self.output_directory = output_directory
        self.working_directory: str | None = None
        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self.budget_config = budget_config or BudgetConfig()
        self.system_limits = system_limits
        self.worker_shortcut_probability = worker_shortcut_probability
        self.budget_threshold_ratio = budget_threshold_ratio
        self.initial_boss_budget: float = 0.0
        self._workspace_context_cache: str | None = None
        self._workspace_context_scanned: bool = False

        # Query service for left-to-right worker execution ordering
        self._query_service = QueryService(event_store)

        # Set subordinate models for multi-model strategy
        self.subordinate_models = subordinate_models or DEFAULT_SUBORDINATE_MODELS

        if model_config is None:
            model_config = {
                "boss": "o3-mini",
                "manager": "o3-mini",
                "worker": "o3-mini",
                "pending": "o3-mini",
            }
        self.model_config = model_config

    def _notify_progress(self, event: DomainEvent, agent: AgentSession) -> None:
        if self.progress_callback is not None:
            with contextlib.suppress(Exception):
                self.progress_callback(event, agent)

    def _get_workspace_context(self) -> str | None:
        """Return cached file listing for workspace. Enables workers to see each other's files."""
        if self._workspace_context_scanned:
            return self._workspace_context_cache

        self._workspace_context_scanned = True

        if not self.working_directory:
            return None

        workspace_path = Path(self.working_directory)
        if not workspace_path.exists():
            return None

        try:
            files = []
            for item in workspace_path.rglob("*"):
                if any(part.startswith(".") for part in item.parts):
                    continue
                if item.is_file():
                    rel_path = item.relative_to(workspace_path)
                    files.append(str(rel_path))

            if not files:
                return None

            files.sort()
            self._workspace_context_cache = "\n".join(f"- {f}" for f in files)
            return self._workspace_context_cache

        except OSError:
            return None

    async def _get_relevant_context(
        self,
        agent: AgentSession,
        max_entries: int = 5,
    ) -> tuple[str | None, list[UUID], int]:
        """Query context dashboard for relevant previous work.

        Source context (key information from the Boss prompt) is ALWAYS included
        for workers in the same session. Worker context (previous work) uses
        LLM to evaluate relevance.

        Args:
            agent: The worker agent that needs context.
            max_entries: Maximum number of worker context entries to include.

        Returns:
            Tuple of (formatted_context, entry_ids, total_available):
            - formatted_context: Context string or None if no relevant context
            - entry_ids: List of inherited entry UUIDs (includes source context)
            - total_available: Total entries available in the dashboard
        """
        if self.context_dashboard is None:
            return None, [], 0

        try:
            all_entries = []
            all_entry_ids = []

            # 1. ALWAYS include source context from the current session
            # This contains key information from the Boss's original prompt
            root_session_id = await self._get_root_session_id(agent.session_id)
            source_entries = await self.context_dashboard.get_source_context_by_session(
                root_session_id
            )
            for entry in source_entries:
                all_entries.append(entry)
                if entry.entry_id:
                    all_entry_ids.append(entry.entry_id)

            # 2. Get worker context using LLM relevance matching
            titles = await self.context_dashboard.get_all_titles()
            total_available = len(titles)

            if titles and self.prompt_builder:
                # Filter out source context entries (already included) and limit
                worker_titles = [
                    (eid, title) for eid, title in titles
                    if not title.startswith("[Source Context]")
                ][:100]

                if worker_titles:
                    # Build relevance query prompt
                    prompt = self.prompt_builder.build_context_relevance_prompt(
                        task_description=agent.task_description,
                        available_titles=worker_titles,
                        justification=agent.supervisor_justification,
                        max_entries=max_entries,
                    )

                    # Query LLM for relevant entry IDs
                    llm_config = {"model": "gpt-4o-mini", "temperature": 0.3, "max_tokens": 500}
                    response = await self.llm_port.query(prompt, llm_config)

                    # Parse response to get entry IDs
                    relevant_ids = self._parse_relevant_ids(response, max_entries)

                    if relevant_ids:
                        # Fetch full worker entries
                        worker_entries = await self.context_dashboard.get_entries_by_ids(
                            relevant_ids
                        )
                        for entry in worker_entries:
                            all_entries.append(entry)
                            if entry.entry_id and entry.entry_id not in all_entry_ids:
                                all_entry_ids.append(entry.entry_id)

            # If no entries found at all, return empty
            if not all_entries:
                return None, [], total_available

            # Format as context string (pass task description for CWE pattern selection)
            return self._format_context_for_prompt(
                all_entries, task_description=agent.task_description
            ), all_entry_ids, total_available

        except Exception:
            # Don't fail worker execution if context retrieval fails
            return None, [], 0

    def _parse_relevant_ids(self, response: str, max_entries: int) -> list[UUID]:
        """Parse LLM response to extract relevant entry IDs."""
        import json
        import re

        # Extract JSON array from response
        match = re.search(r"\[.*?\]", response, re.DOTALL)
        if not match:
            return []

        try:
            ids = json.loads(match.group())
            return [UUID(id_str) for id_str in ids[:max_entries] if id_str]
        except (json.JSONDecodeError, ValueError):
            return []

    def _format_context_for_prompt(
        self,
        entries: list,  # list[ContextEntry]
        task_description: str = "",
    ) -> str:
        """Format context entries for injection into worker prompt.

        Provides comprehensive context from previous work including:
        - What was done (objective, approach)
        - How it was accomplished (work_analysis)
        - What challenges were encountered (to avoid repeating mistakes)
        - Clear reminders about not duplicating work

        Also handles SOURCE type entries which contain key information
        extracted from the Boss's original prompt.

        Args:
            entries: List of context entries to format.
            task_description: The worker's task description, used to determine
                whether to include PoC or patch fix patterns.
        """
        from core.domain.context_entry import ContextEntryType

        if not entries:
            return ""

        # Separate source context from worker context
        source_entries = [e for e in entries if e.entry_type == ContextEntryType.SOURCE]
        worker_entries = [e for e in entries if e.entry_type == ContextEntryType.WORKER]

        lines = [
            "<RELEVANT_CONTEXT>",
            "=" * 60,
        ]

        # Include source context first (key info from original prompt)
        if source_entries:
            lines.append("ORIGINAL TASK KEY INFORMATION")
            lines.append("=" * 60)
            lines.append("")
            lines.append("The following key information was extracted from the original task:")
            lines.append("")

            for entry in source_entries:
                if entry.source_context:
                    sc = entry.source_context

                    if sc.bug_summary:
                        lines.append("## Bug/Issue Summary")
                        lines.append(sc.bug_summary)
                        lines.append("")

                    if sc.error_messages:
                        lines.append("## Error Messages")
                        for err in sc.error_messages:
                            lines.append(f"- {err}")
                        lines.append("")

                    if sc.reproduction_steps:
                        lines.append("## Reproduction Steps")
                        lines.append(sc.reproduction_steps)
                        lines.append("")

                    if sc.file_paths:
                        lines.append("## Referenced Files")
                        for fp in sc.file_paths:
                            lines.append(f"- {fp}")
                        lines.append("")

                    if sc.commit_references:
                        lines.append("## Version/Commit References")
                        for ref in sc.commit_references:
                            lines.append(f"- {ref}")
                        lines.append("")

                    if sc.urls:
                        lines.append("## Related URLs")
                        for url in sc.urls:
                            lines.append(f"- {url}")
                        lines.append("")

                    if sc.environment:
                        lines.append("## Environment")
                        lines.append(sc.environment)
                        lines.append("")

                    if sc.dependencies:
                        lines.append("## Dependencies")
                        for dep in sc.dependencies:
                            lines.append(f"- {dep}")
                        lines.append("")

                    if sc.key_facts:
                        lines.append("## Key Facts & Requirements")
                        for fact in sc.key_facts:
                            lines.append(f"- {fact}")
                        lines.append("")

                    # CWE Pattern Information (inferred by BOSS from bug analysis)
                    if sc.inferred_cwes:
                        lines.append("## ⚠️ INFERRED CWE PATTERNS")
                        lines.append("The following CWE patterns were identified from the bug report:")
                        lines.append("")
                        for cwe in sc.inferred_cwes:
                            confidence = sc.cwe_confidence.get(cwe, "unknown")
                            reasoning = sc.cwe_reasoning.get(cwe, "")
                            fix_pattern = sc.fix_patterns.get(cwe, "")
                            lines.append(f"### {cwe} (Confidence: {confidence})")
                            if reasoning:
                                lines.append(f"**Why this CWE:** {reasoning}")
                            if fix_pattern:
                                lines.append(f"**Recommended Fix Pattern:** {fix_pattern}")
                            lines.append("")

                    if sc.recommended_sanitizers:
                        lines.append("## Recommended Sanitizers for Verification")
                        for san in sc.recommended_sanitizers:
                            lines.append(f"- {san}")
                        lines.append("")

            # Collect all inferred CWEs across all source entries
            all_cwes: list[str] = []
            for entry in source_entries:
                if entry.source_context and entry.source_context.inferred_cwes:
                    all_cwes.extend(entry.source_context.inferred_cwes)

            # Remove duplicates while preserving order
            seen: set[str] = set()
            unique_cwes: list[str] = []
            for cwe in all_cwes:
                if cwe not in seen:
                    seen.add(cwe)
                    unique_cwes.append(cwe)

            # Add detailed CWE fix patterns from security knowledge base
            if unique_cwes and self.prompt_builder:
                # Determine task type based on task description keywords
                task_lower = task_description.lower() if task_description else ""

                if "poc" in task_lower or "exploit" in task_lower or "trigger" in task_lower:
                    task_type = "poc"
                else:
                    task_type = "patch"

                cwe_guidance = self.prompt_builder.get_cwe_guidance_for_worker(
                    inferred_cwes=unique_cwes,
                    task_type=task_type,
                )
                if cwe_guidance:
                    lines.append(cwe_guidance)

            lines.append("-" * 60)
            lines.append("")

        # Include worker context (previous work)
        if worker_entries:
            lines.append("KNOWLEDGE FROM PREVIOUS SESSIONS")
            lines.append("=" * 60)
            lines.append("")
            lines.append("IMPORTANT REMINDERS:")
            lines.append("1. DO NOT repeat work that has already been completed below")
            lines.append("2. DO NOT repeat the same mistakes - learn from the challenges encountered")
            lines.append("3. BUILD UPON the successful approaches - leverage what worked")
            lines.append("4. REFERENCE this context when making decisions about your approach")
            lines.append("")
            lines.append("-" * 60)

            for i, entry in enumerate(worker_entries, 1):
                lines.append(f"\n## [{i}] {entry.work_title}")
                lines.append("")
                lines.append(f"**Objective:** {entry.objective}")
                lines.append("")

                # Include the comprehensive work analysis
                if entry.work_analysis:
                    lines.append("**How This Work Was Accomplished:**")
                    lines.append(entry.work_analysis)
                    lines.append("")

                # Include the approach
                if entry.worker_report and entry.worker_report.approach:
                    lines.append(f"**Approach Used:** {entry.worker_report.approach}")
                    lines.append("")

                # Include reasoning if available
                if entry.worker_report and entry.worker_report.reasoning:
                    lines.append(f"**Reasoning:** {entry.worker_report.reasoning}")
                    lines.append("")

                # Include deliverables to show what was already produced
                if entry.worker_report and entry.worker_report.deliverables:
                    lines.append(f"**Deliverables Produced:** {entry.worker_report.deliverables}")
                    lines.append("")

                # Highlight challenges as warnings
                if entry.worker_report and entry.worker_report.challenges:
                    lines.append("**⚠️ CHALLENGES TO AVOID (Learn from these mistakes):**")
                    lines.append(entry.worker_report.challenges)
                    lines.append("")

                # Include tags for context
                if entry.tags:
                    lines.append(f"**Tags:** {', '.join(entry.tags)}")
                    lines.append("")

                lines.append("-" * 60)

        lines.append("")
        lines.append("=" * 60)
        lines.append("END OF CONTEXT")
        lines.append("=" * 60)
        lines.append("")
        lines.append("Use the above context to:")
        if source_entries:
            lines.append("- Reference key information from the original task")
        if worker_entries:
            lines.append("- Avoid duplicating completed work")
            lines.append("- Learn from challenges and avoid repeating mistakes")
            lines.append("- Build upon successful approaches and patterns")
        lines.append("- Make informed decisions based on the full context")
        lines.append("")
        lines.append("</RELEVANT_CONTEXT>")

        return "\n".join(lines)

    async def _extract_and_publish_source_context(
        self,
        boss: AgentSession,
        task_description: str,
    ) -> None:
        """Extract key information from Boss prompt and publish to context dashboard.

        Called immediately after creating the Boss agent. Uses LLM to extract
        key references (bug reports, file paths, commits, error messages, etc.)
        from the original user prompt and publishes them as a SOURCE type context
        entry that all workers can reference.

        Args:
            boss: The Boss agent session.
            task_description: The original user prompt.
        """
        from datetime import UTC, datetime

        from core.domain.context_entry import (
            ContextEntry,
            ContextEntryType,
        )

        if self.context_dashboard is None:
            return

        if self.prompt_builder is None:
            return

        try:
            # First, try direct extraction if task_description contains structured JSON
            direct_data = self._try_direct_json_extraction(task_description)

            # Build the extraction prompt
            prompt = self.prompt_builder.build_source_context_extraction_prompt(
                task_description=task_description,
            )

            # Query LLM to extract key information
            # Use higher max_tokens to accommodate large outputs (dockerfile, build scripts, etc.)
            llm_config = {"model": "gpt-4o-mini", "temperature": 0.2, "max_tokens": 4000}
            response = await self.llm_port.query(prompt, llm_config)

            # Parse the JSON response
            source_data = self._parse_source_context_response(response)

            # Merge direct extraction data if LLM extraction is incomplete
            if direct_data is not None:
                source_data = self._merge_source_context(source_data, direct_data)

            # Only publish if we found meaningful content
            if not self._has_meaningful_source_context(source_data):
                print("   ⚠️  Source context extraction skipped: no meaningful content found")
                return

            # Generate a title for this source context
            work_title = self._generate_source_context_title(source_data, task_description)

            # Create the context entry
            entry = ContextEntry(
                entry_type=ContextEntryType.SOURCE,
                work_title=work_title,
                objective="Key information from the original task prompt",
                justification="Extracted to provide workers with full context",
                work_analysis="",  # Not applicable for source context
                worker_report=None,  # Not applicable for source context
                source_context=source_data,
                session_id=boss.session_id,
                worker_id=boss.session_id,  # Boss is the "source" for source context
                created_at=datetime.now(UTC),
                tags=self._extract_tags(task_description),
            )

            # Publish to dashboard
            entry_id = await self.context_dashboard.publish(entry)

            # Record the extraction event on the Boss (includes CWE patterns)
            boss.record_source_context_extracted(
                context_entry_id=entry_id,
                extraction_summary=work_title,
                key_references=source_data.file_paths + source_data.commit_references,
                has_bug_report=bool(source_data.bug_summary),
                has_error_details=bool(source_data.error_messages),
                has_file_references=bool(source_data.file_paths),
                # CWE pattern inference - published to dashboard for visibility
                inferred_cwes=source_data.inferred_cwes,
                cwe_reasoning=source_data.cwe_reasoning,
                recommended_sanitizers=source_data.recommended_sanitizers,
                fix_patterns=source_data.fix_patterns,
            )

            # Show extraction success
            print(f"   ✓ Source context extracted: {work_title[:60]}...")

        except Exception as e:
            # Don't fail Boss creation if context extraction fails
            print(f"   ⚠️  Source context extraction failed: {e}")

    def _try_direct_json_extraction(self, task_description: str):
        """Try to extract source context directly from structured JSON in task description.

        If the task description contains a JSON blob with fields like 'data.dockerfile',
        'data.build_sh', etc., extract them directly without LLM interpretation.

        Args:
            task_description: The original task description.

        Returns:
            SourceContextData if structured data found, None otherwise.
        """
        import json
        import re

        from core.domain.context_entry import SourceContextData

        # Try to find and parse JSON in the task description
        json_match = re.search(r"\{[\s\S]*\}", task_description)
        if not json_match:
            return None

        try:
            data = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            return None

        # Check if this is a security benchmark format with 'data' field
        inner = data.get("data", data)
        if not isinstance(inner, dict):
            return None

        # Extract instance_id to get CVE ID
        instance_id = inner.get("instance_id", "")
        cve_match = re.search(r"(cve-\d{4}-\d+)", instance_id, re.IGNORECASE)
        cve_id = cve_match.group(1).upper().replace("CVE-", "CVE-") if cve_match else ""

        # Extract file paths from various fields
        file_paths = []
        work_dir = inner.get("work_dir", "")
        if work_dir:
            file_paths.append(work_dir)

        # Extract URLs
        urls = []
        repo = inner.get("repo", "")
        if repo:
            urls.append(f"https://github.com/{repo}")

        # Extract commit references
        commit_refs = []
        base_commit = inner.get("base_commit", "")
        if base_commit:
            commit_refs.append(base_commit)

        # Extract error messages from sanitizer_report
        error_messages = []
        sanitizer_report = inner.get("sanitizer_report", "")
        if sanitizer_report:
            # Extract key lines from sanitizer report
            lines = sanitizer_report.split("\n")
            for line in lines[:20]:  # First 20 lines
                if "ERROR:" in line or "SUMMARY:" in line or "#" in line:
                    error_messages.append(line.strip())

        # Infer CWE from bug description and sanitizer report
        inferred_cwes = []
        cwe_reasoning = {}
        bug_desc = inner.get("bug_description", "")
        combined_text = f"{bug_desc} {sanitizer_report}".lower()

        if "segv" in combined_text and "0x000000000000" in combined_text:
            inferred_cwes.append("CWE-476")
            cwe_reasoning["CWE-476"] = "SEGV on address 0x0 indicates NULL pointer dereference"
        if "heap-buffer-overflow" in combined_text:
            inferred_cwes.append("CWE-787")
            cwe_reasoning["CWE-787"] = "Heap buffer overflow detected by AddressSanitizer"
        if "use-after-free" in combined_text:
            inferred_cwes.append("CWE-416")
            cwe_reasoning["CWE-416"] = "Use-after-free detected by AddressSanitizer"
        if "double-free" in combined_text:
            inferred_cwes.append("CWE-415")
            cwe_reasoning["CWE-415"] = "Double-free detected by AddressSanitizer"

        # Extract PoC command from secb_sh if present
        poc_command = ""
        secb_sh = inner.get("secb_sh", "")
        if secb_sh:
            # Look for the repro function content
            repro_match = re.search(r"repro\(\)\s*\{([^}]+)\}", secb_sh, re.DOTALL)
            if repro_match:
                repro_content = repro_match.group(1)
                # Find the command line (line not starting with echo or #)
                for line in repro_content.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("echo") and not line.startswith("#"):
                        poc_command = line
                        break

        print(f"   ℹ️  Direct extraction: CVE={cve_id}, CWEs={inferred_cwes}, dockerfile={'yes' if inner.get('dockerfile') else 'no'}")

        return SourceContextData(
            bug_summary=inner.get("bug_description", ""),
            error_messages=error_messages,
            file_paths=file_paths,
            commit_references=commit_refs,
            urls=urls,
            dockerfile=inner.get("dockerfile", ""),
            build_script=inner.get("build_sh", ""),
            work_dir=work_dir,
            poc_command=poc_command,
            sanitizer=inner.get("sanitizer", ""),
            cve_id=cve_id,
            repo_url=f"https://github.com/{repo}" if repo else "",
            inferred_cwes=inferred_cwes,
            cwe_reasoning=cwe_reasoning,
            key_facts=[
                f"Project: {inner.get('project_name', '')}",
                f"Language: {inner.get('lang', '')}",
            ],
        )

    def _merge_source_context(self, llm_data, direct_data):
        """Merge LLM-extracted data with directly extracted data.

        Direct extraction takes precedence for structured fields (dockerfile, build_script, etc.)
        LLM extraction takes precedence for inferred/analyzed fields (bug_summary, CWE reasoning).

        Args:
            llm_data: SourceContextData from LLM extraction.
            direct_data: SourceContextData from direct JSON extraction.

        Returns:
            Merged SourceContextData.
        """
        from core.domain.context_entry import SourceContextData

        # For structured fields, prefer direct extraction (more reliable)
        # For inferred fields, prefer LLM extraction (more intelligent)
        return SourceContextData(
            # LLM is better at summarizing
            bug_summary=llm_data.bug_summary or direct_data.bug_summary,
            # Merge error messages
            error_messages=llm_data.error_messages or direct_data.error_messages,
            reproduction_steps=llm_data.reproduction_steps or direct_data.reproduction_steps,
            # Merge and deduplicate paths
            file_paths=list(set(llm_data.file_paths + direct_data.file_paths)),
            commit_references=list(set(llm_data.commit_references + direct_data.commit_references)),
            urls=list(set(llm_data.urls + direct_data.urls)),
            environment=llm_data.environment or direct_data.environment,
            dependencies=llm_data.dependencies or direct_data.dependencies,
            key_facts=list(set(llm_data.key_facts + direct_data.key_facts)),
            # Direct extraction is more reliable for structured fields
            dockerfile=direct_data.dockerfile or llm_data.dockerfile,
            build_script=direct_data.build_script or llm_data.build_script,
            work_dir=direct_data.work_dir or llm_data.work_dir,
            poc_command=direct_data.poc_command or llm_data.poc_command,
            sanitizer=direct_data.sanitizer or llm_data.sanitizer,
            cve_id=direct_data.cve_id or llm_data.cve_id,
            repo_url=direct_data.repo_url or llm_data.repo_url,
            # Merge CWE inferences (direct + LLM)
            inferred_cwes=list(set(llm_data.inferred_cwes + direct_data.inferred_cwes)),
            cwe_reasoning={**direct_data.cwe_reasoning, **llm_data.cwe_reasoning},
            cwe_confidence=llm_data.cwe_confidence or direct_data.cwe_confidence,
            recommended_sanitizers=llm_data.recommended_sanitizers or direct_data.recommended_sanitizers,
            fix_patterns={**direct_data.fix_patterns, **llm_data.fix_patterns},
            original_prompt=llm_data.original_prompt or direct_data.original_prompt,
        )

    def _parse_source_context_response(self, response: str):
        """Parse LLM response to extract SourceContextData."""
        import json
        import re

        from core.domain.context_entry import SourceContextData

        # Extract JSON from response (may be wrapped in markdown code block)
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", response)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try to find raw JSON object
            json_match = re.search(r"\{[\s\S]*\}", response)
            if json_match:
                json_str = json_match.group(0)
            else:
                print(f"   ⚠️  No JSON found in LLM response (len={len(response)})")
                # Fallback: create minimal context with raw response
                return self._create_fallback_source_context(response)

        try:
            data = json.loads(json_str)
            return SourceContextData(
                bug_summary=data.get("bug_summary", ""),
                error_messages=data.get("error_messages", []),
                reproduction_steps=data.get("reproduction_steps", ""),
                file_paths=data.get("file_paths", []),
                commit_references=data.get("commit_references", []),
                urls=data.get("urls", []),
                environment=data.get("environment", ""),
                dependencies=data.get("dependencies", []),
                key_facts=data.get("key_facts", []),
                # Security/CVE build context fields
                dockerfile=data.get("dockerfile", ""),
                build_script=data.get("build_script", ""),
                work_dir=data.get("work_dir", ""),
                poc_command=data.get("poc_command", ""),
                sanitizer=data.get("sanitizer", ""),
                cve_id=data.get("cve_id", ""),
                repo_url=data.get("repo_url", ""),
                # CWE pattern inference fields
                inferred_cwes=data.get("inferred_cwes", []),
                cwe_reasoning=data.get("cwe_reasoning", {}),
                cwe_confidence=data.get("cwe_confidence", {}),
                recommended_sanitizers=data.get("recommended_sanitizers", []),
                fix_patterns=data.get("fix_patterns", {}),
                original_prompt=response[:5000],  # Preserve original for reference
            )
        except json.JSONDecodeError as e:
            print(f"   ⚠️  JSON parse error in source context: {e}")
            # Fallback: try to extract basic info from truncated/malformed JSON
            return self._create_fallback_source_context(response, json_str)

    def _create_fallback_source_context(self, response: str, partial_json: str = ""):
        """Create fallback SourceContextData by extracting patterns from raw text.

        Called when JSON parsing fails. Uses regex to extract key information
        from the original task or partial LLM response.

        Args:
            response: The full LLM response.
            partial_json: The partial/malformed JSON string if available.
        """
        import re

        from core.domain.context_entry import SourceContextData

        # Extract patterns using regex from response or partial JSON
        text = partial_json or response

        # Extract CVE ID (e.g., CVE-2024-50665)
        cve_match = re.search(r"(CVE-\d{4}-\d+)", text, re.IGNORECASE)
        cve_id = cve_match.group(1).upper() if cve_match else ""

        # Extract CWE patterns (e.g., CWE-476, CWE-787)
        cwe_matches = re.findall(r"(CWE-\d+)", text, re.IGNORECASE)
        inferred_cwes = list(set(cwe.upper() for cwe in cwe_matches))

        # Extract file paths (e.g., /src/file.c, src/component.ts)
        file_patterns = re.findall(r'["\']?(/[\w/.-]+\.\w+)["\']?', text)
        file_paths = list(set(file_patterns[:10]))  # Limit to 10

        # Extract URLs
        url_matches = re.findall(r'https?://[^\s"\'<>]+', text)
        urls = list(set(url_matches[:5]))  # Limit to 5

        # Extract commit hashes (7-40 hex chars)
        commit_matches = re.findall(r'\b([a-f0-9]{7,40})\b', text.lower())
        commit_refs = list(set(commit_matches[:5]))  # Limit to 5

        # Try to extract dockerfile content
        dockerfile = ""
        dockerfile_match = re.search(
            r'"dockerfile"\s*:\s*"([^"]*(?:\\.[^"]*)*)"', text
        )
        if dockerfile_match:
            dockerfile = dockerfile_match.group(1).replace("\\n", "\n")

        # Try to extract build_script content
        build_script = ""
        build_match = re.search(
            r'"build_script"\s*:\s*"([^"]*(?:\\.[^"]*)*)"', text
        )
        if build_match:
            build_script = build_match.group(1).replace("\\n", "\n")

        # Try to extract work_dir
        work_dir = ""
        workdir_match = re.search(r'"work_dir"\s*:\s*"([^"]+)"', text)
        if workdir_match:
            work_dir = workdir_match.group(1)

        # Try to extract sanitizer
        sanitizer = ""
        sanitizer_match = re.search(r'"sanitizer"\s*:\s*"([^"]+)"', text)
        if sanitizer_match:
            sanitizer = sanitizer_match.group(1)

        # Try to extract bug_summary
        bug_summary = ""
        summary_match = re.search(r'"bug_summary"\s*:\s*"([^"]*(?:\\.[^"]*)*)"', text)
        if summary_match:
            bug_summary = summary_match.group(1).replace("\\n", " ")[:500]

        # Build key_facts from what we extracted
        key_facts = []
        if cve_id:
            key_facts.append(f"CVE: {cve_id}")
        if inferred_cwes:
            key_facts.append(f"CWE patterns: {', '.join(inferred_cwes)}")
        if not key_facts:
            key_facts.append("Fallback extraction due to JSON parse error")

        print(f"   ℹ️  Fallback extracted: CVE={cve_id}, CWEs={inferred_cwes}, files={len(file_paths)}")

        return SourceContextData(
            bug_summary=bug_summary,
            file_paths=file_paths,
            commit_references=commit_refs,
            urls=urls,
            cve_id=cve_id,
            inferred_cwes=inferred_cwes,
            dockerfile=dockerfile,
            build_script=build_script,
            work_dir=work_dir,
            sanitizer=sanitizer,
            key_facts=key_facts,
            original_prompt=response[:5000],
        )

    def _has_meaningful_source_context(self, data) -> bool:
        """Check if the extracted source context has meaningful content.

        Args:
            data: SourceContextData instance to check.
        """
        # Consider it meaningful if it has any of these populated
        return bool(
            data.bug_summary
            or data.error_messages
            or data.file_paths
            or data.commit_references
            or data.urls
            or data.key_facts
            # Security/CVE build context fields
            or data.dockerfile
            or data.build_script
            or data.cve_id
            or data.poc_command
            # CWE pattern inference fields
            or data.inferred_cwes
        )

    def _generate_source_context_title(
        self,
        data,
        task_description: str,
    ) -> str:
        """Generate a descriptive title for source context entry."""
        parts = []

        # Security/CVE context takes priority
        if data.cve_id:
            parts.append(data.cve_id)
        # CWE patterns - high priority for security tasks
        if data.inferred_cwes:
            cwe_str = "/".join(data.inferred_cwes[:3])  # Show up to 3 CWEs
            parts.append(f"[{cwe_str}]")
        if data.dockerfile or data.build_script:
            parts.append("Build Context")
        if data.poc_command:
            parts.append("PoC Command")
        if data.bug_summary:
            parts.append("Bug Report")
        if data.error_messages:
            parts.append("Error Details")
        if data.file_paths:
            parts.append(f"{len(data.file_paths)} Files")
        if data.commit_references:
            parts.append("Commits/Versions")
        if data.urls:
            parts.append("References")

        if parts:
            title = f"[Source Context] {', '.join(parts)}"
        else:
            # Fallback: extract first meaningful words from task
            words = task_description.split()[:8]
            title = f"[Source Context] {' '.join(words)}..."

        # Ensure max 100 chars for better CWE visibility
        if len(title) > 100:
            title = title[:97] + "..."

        return title

    async def _publish_worker_context(
        self,
        parent: AgentSession,
        worker: AgentSession,
    ) -> None:
        """Publish completed worker context to the global dashboard.

        Called when a supervisor receives a completed worker report.

        Args:
            parent: The supervisor (BOSS/MANAGER) that received the completion.
            worker: The worker that completed the task.
        """
        from datetime import UTC, datetime

        from core.domain.context_entry import ContextEntry

        if self.context_dashboard is None:
            return

        # Get supervisor's justification for this worker
        justification = worker.supervisor_justification
        if justification is None or worker.worker_report is None:
            return  # Skip if no justification or report

        # Generate concise, informative key using LLM
        work_title = await self._generate_work_title(
            justification, worker.task_description, worker.worker_report
        )

        # Synthesize comprehensive work analysis using LLM
        work_analysis = await self._synthesize_work_analysis(
            justification, worker.task_description, worker.worker_report
        )

        # Get root session ID (BOSS) - walk up the tree
        root_id = await self._get_root_session_id(worker.session_id)

        entry = ContextEntry(
            work_title=work_title,
            objective=justification.objective,
            justification=justification.split_reason,
            work_analysis=work_analysis,
            worker_report=worker.worker_report,
            session_id=root_id,
            worker_id=worker.session_id,
            created_at=datetime.now(UTC),
            tags=self._extract_tags(worker.task_description),
        )

        try:
            entry_id = await self.context_dashboard.publish(entry)

            # Record audit event on parent
            parent.record_context_published(
                entry_id=entry_id,
                work_title=work_title,
                worker_id=worker.session_id,
                objective=justification.objective,
                justification_summary=justification.split_reason[:500],
            )
        except Exception:
            # Don't fail parent notification if context publishing fails
            pass

    async def _generate_work_title(
        self,
        justification,  # SubtaskJustification
        task_description: str,
        worker_report,  # WorkerReport
    ) -> str:
        """Generate a concise, informative key for the context dashboard using LLM.

        The key should be short (max 80 chars) but capture the essence of the
        completed work so other agents can quickly identify relevant entries.
        """
        if self.prompt_builder is None:
            # Fallback: use objective truncated
            title = justification.objective
            if len(title) > 80:
                title = title[:77] + "..."
            return title

        try:
            prompt = self.prompt_builder.build_context_key_prompt(
                task_description=task_description,
                objective=justification.objective,
                deliverables=worker_report.deliverables or "",
                approach=worker_report.approach or "",
            )

            response = await self.llm_port.generate(
                prompt=prompt,
                model="gpt-4o-mini",  # Use GPT model for context key generation
                max_tokens=100,
            )

            # Clean up the response - take first line, strip quotes
            key = response.strip().strip('"\'').split('\n')[0]
            # Ensure max 80 chars
            if len(key) > 80:
                key = key[:77] + "..."
            return key

        except Exception:
            # Fallback on error
            title = justification.objective
            if len(title) > 80:
                title = title[:77] + "..."
            return title

    async def _synthesize_work_analysis(
        self,
        justification,  # SubtaskJustification
        task_description: str,
        worker_report,  # WorkerReport
    ) -> str:
        """Synthesize comprehensive work analysis using LLM.

        Creates a detailed analysis of HOW the work was accomplished,
        including implementation details, lessons learned, and reusable patterns.
        """
        if self.prompt_builder is None:
            # Fallback: basic concatenation
            return self._basic_work_analysis(worker_report)

        try:
            prompt = self.prompt_builder.build_context_analysis_prompt(
                task_description=task_description,
                objective=justification.objective,
                justification=justification.split_reason,
                approach=worker_report.approach or "",
                reasoning=worker_report.reasoning or "",
                deliverables=worker_report.deliverables or "",
                challenges=worker_report.challenges or "",
                observations=worker_report.observations or "",
                fulfillment_evidence=worker_report.fulfillment_evidence or "",
            )

            response = await self.llm_port.generate(
                prompt=prompt,
                model="gpt-4o-mini",  # Use GPT model for context analysis
                max_tokens=800,
            )

            return response.strip()

        except Exception:
            # Fallback on error
            return self._basic_work_analysis(worker_report)

    def _basic_work_analysis(self, report) -> str:  # report: WorkerReport
        """Basic work analysis fallback when LLM is unavailable."""
        parts = []
        if report.approach:
            parts.append(f"**Approach:** {report.approach}")
        if report.reasoning:
            parts.append(f"**Reasoning:** {report.reasoning}")
        if report.deliverables:
            parts.append(f"**Deliverables:** {report.deliverables}")
        if report.challenges:
            parts.append(f"**Challenges:** {report.challenges}")
        if report.observations:
            parts.append(f"**Observations:** {report.observations}")
        if report.fulfillment_evidence:
            parts.append(f"**Fulfillment Evidence:** {report.fulfillment_evidence}")
        return "\n\n".join(parts)

    def _extract_tags(self, task_description: str) -> list[str]:
        """Extract relevant tags from task description."""
        tags = []
        keywords = [
            "python",
            "javascript",
            "typescript",
            "react",
            "api",
            "database",
            "security",
            "test",
            "docker",
            "config",
        ]
        lower_desc = task_description.lower()
        for keyword in keywords:
            if keyword in lower_desc:
                tags.append(keyword)
        return tags[:5]  # Limit to 5 tags

    async def _get_root_session_id(self, session_id: UUID) -> UUID:
        """Walk up the parent chain to find the root (BOSS) session ID."""
        current_id = session_id
        visited = {current_id}

        while True:
            events = await self.event_store.get_events(current_id)
            if not events:
                return current_id  # Can't find parent, return current

            agent = AgentSession.load_from_history(events)
            if agent.parent_id is None:
                return current_id  # Found the root

            if agent.parent_id in visited:
                return current_id  # Cycle detection

            visited.add(agent.parent_id)
            current_id = agent.parent_id

    async def _flush_and_notify_events(
        self, agent: AgentSession, current_version: int
    ) -> int:
        """Flush uncommitted events to event store and notify progress.

        Args:
            agent: The agent with uncommitted events to flush.
            current_version: The expected version before the first event.

        Returns:
            The updated version after all events are committed.
        """
        uncommitted_events = list(agent.events)
        for event in uncommitted_events:
            await self.event_store.append(event, expected_version=current_version)
            current_version += 1
            self._notify_progress(event, agent)
        agent.mark_changes_as_committed()
        return current_version

    async def run_agent_step(self, agent_id: UUID) -> None:
        """Execute one workflow step: load → dispatch → persist with OCC retry."""
        retry_count = 0

        while retry_count < self.max_retries:
            try:
                events = await self.event_store.get_events(agent_id)
                if not events:
                    raise ValueError(f"Agent {agent_id} not found in event store")

                agent = AgentSession.load_from_history(events)
                current_version = agent.version

                # Dispatch may flush events mid-execution (e.g., ContextInherited for workers)
                # so we need to track the updated version
                current_version = await self._dispatch_agent_action(agent, current_version)

                uncommitted_events = list(agent.events)
                for event in uncommitted_events:
                    await self.event_store.append(event, expected_version=current_version)
                    current_version += 1
                    self._notify_progress(event, agent)

                agent.mark_changes_as_committed()
                await self._handle_child_spawning(agent, uncommitted_events)
                await self._handle_parent_notification(agent)
                return

            except ConcurrencyError as e:
                retry_count += 1
                if retry_count >= self.max_retries:
                    raise ConcurrencyError(
                        aggregate_id=str(agent_id),
                        expected_version=e.expected_version,
                        actual_version=e.actual_version,
                    ) from e

            except Exception as e:
                try:
                    events = await self.event_store.get_events(agent_id)
                    if events:
                        agent = AgentSession.load_from_history(events)
                        current_version = agent.version
                        agent.fail_with_reason(f"Execution error: {e!r}")

                        for event in agent.events:
                            await self.event_store.append(event, expected_version=current_version)
                            current_version += 1

                except Exception as persist_error:
                    raise RuntimeError(
                        f"Failed to persist error state for agent {agent_id}: {persist_error!r}"
                    ) from e
                raise

    async def _dispatch_agent_action(
        self, agent: AgentSession, current_version: int
    ) -> int:
        """Dispatch based on role: PENDING→complexity, BOSS/MANAGER→decompose, WORKER→execute.

        Args:
            agent: The agent to dispatch.
            current_version: The current committed version, used for mid-execution event flushing.

        Returns:
            The updated version after any mid-execution event flushing.
        """
        # Recovery: if WAITING parent has all children done, process missing notifications
        if agent.status == AgentStatus.WAITING and agent.child_ids:
            await self._recover_stuck_parent(agent)
            return current_version

        if agent.status != AgentStatus.ANALYZING:
            return current_version

        if agent.role == AgentRole.PENDING:
            # Shortcut to worker if: budget below threshold OR random chance
            budget_threshold = self.initial_boss_budget * self.budget_threshold_ratio
            if agent.current_budget < budget_threshold:
                pct = self.budget_threshold_ratio * 100
                agent.shortcut_to_worker(f"budget below {pct:.0f}% of initial allocation")
            elif random.random() < self.worker_shortcut_probability:
                agent.shortcut_to_worker("randomly selected to skip complexity evaluation")
            else:
                await agent.evaluate_complexity(self.llm_port, self.prompt_builder)

        elif agent.role in (AgentRole.BOSS, AgentRole.MANAGER):
            await agent.evaluate_task(self.llm_port, self.prompt_builder)

        elif agent.role == AgentRole.WORKER:
            workspace_context = self._get_workspace_context()
            # Query context dashboard for relevant cross-session context
            cross_session_context, inherited_ids, total_available = (
                await self._get_relevant_context(agent)
            )

            # Record inherited context event (even if none inherited, to show in UI)
            agent.record_context_inherited(inherited_ids, total_available)

            # Flush ContextInherited event to UI BEFORE starting execution
            # This allows the Dashboard to show what context will be used
            current_version = await self._flush_and_notify_events(agent, current_version)

            await agent.execute_task(
                self.worker_tool_port,
                working_directory=self.working_directory,
                workspace_context=workspace_context,
                cross_session_context=cross_session_context,
            )

        else:
            raise ValueError(f"Unknown agent role: {agent.role}")

        return current_version

    async def _handle_child_spawning(self, parent: AgentSession, events: list) -> None:
        """Create child AgentSessions from ChildSpawned events."""
        child_spawned_events = [e for e in events if isinstance(e, ChildSpawned)]

        # Calculate weighted budget allocation based on subtask complexity/importance/time
        total_weight = sum(e.subtask.budget_weight for e in child_spawned_events)
        available_budget = parent.current_budget

        for child_index, child_event in enumerate(child_spawned_events):
            child_config = child_event.child_config

            # Compute tree sequence ID for left-to-right worker execution ordering
            # Formula: parent_sequence_id * 1000 + child_index + 1
            # This gives left-to-right ordering across the entire tree
            child_sequence_id = parent.tree_sequence_id * 1000 + child_index + 1

            child = AgentSession.create(
                session_id=child_event.child_id,
                role=AgentRole(child_event.child_role),
                config=child_config,
                parent_id=parent.session_id,
                tree_sequence_id=child_sequence_id,
            )
            child.assign_task(
                child_event.subtask.description,
                justification=child_event.subtask.justification,
            )

            # Allocate budget proportionally based on subtask weight
            if total_weight > 0 and available_budget > 0:
                weight_ratio = child_event.subtask.budget_weight / total_weight
                child_budget = available_budget * weight_ratio
                child.allocate_budget(child_budget, source="parent")

            for idx, child_evt in enumerate(child.events):
                await self.event_store.append(child_evt, expected_version=idx)
                self._notify_progress(child_evt, child)

            child.mark_changes_as_committed()

        # Handle SubordinatesSpawned events (parallel subordinates for same subtask)
        subordinate_events = [e for e in events if isinstance(e, SubordinatesSpawned)]

        for sub_event in subordinate_events:
            for config in sub_event.subordinate_configs:
                if "child_id" not in config:
                    continue

                child_id = config["child_id"]
                if isinstance(child_id, str):
                    child_id = UUID(child_id)

                child_config = config.get("config", {})
                child_role = config.get("role", AgentRole.PENDING.value)

                # Create new subordinate agent
                child = AgentSession.create(
                    session_id=child_id,
                    role=AgentRole(child_role),
                    config=child_config,
                    parent_id=parent.session_id,
                )

                # Assign the subtask to the child with justification
                child.assign_task(
                    sub_event.subtask.description,
                    justification=sub_event.subtask.justification,
                )

                # Allocate budget if specified
                if "budget" in config:
                    child.allocate_budget(config["budget"], source="parent")

                # Persist child's creation events
                for idx, child_evt in enumerate(child.events):
                    await self.event_store.append(child_evt, expected_version=idx)

                child.mark_changes_as_committed()

        # Handle VerifierSpawned events
        verifier_events = [e for e in events if isinstance(e, VerifierSpawned)]

        for verifier_event in verifier_events:
            # Create new verifier agent
            verifier = AgentSession.create(
                session_id=verifier_event.verifier_id,
                role=AgentRole.PENDING,  # Verifier starts as PENDING
                config=verifier_event.verifier_config,
                parent_id=parent.session_id,
            )

            # Assign verification task
            verification_task = (
                f"Verify the following completed task: {verifier_event.target_subtask.description}"
            )
            verifier.assign_task(verification_task)

            # Persist verifier's creation events
            for idx, v_evt in enumerate(verifier.events):
                await self.event_store.append(v_evt, expected_version=idx)

            verifier.mark_changes_as_committed()

    async def _handle_parent_notification(self, agent: AgentSession) -> None:
        """Notify parent when child reaches terminal state (COMPLETED or FAILED)."""
        if agent.parent_id is None:
            return
        if agent.status not in (AgentStatus.COMPLETED, AgentStatus.FAILED):
            return

        parent_events = await self.event_store.get_events(agent.parent_id)
        if not parent_events:
            raise ValueError(f"Parent agent {agent.parent_id} not found in event store")

        parent = AgentSession.load_from_history(parent_events)
        parent_version = parent.version

        if agent.status == AgentStatus.COMPLETED:
            parent.handle_child_update(
                agent.session_id,
                agent.result or "",
                worker_report=agent.worker_report,
            )
            # Publish worker context to dashboard for cross-session learning
            if (
                agent.role == AgentRole.WORKER
                and agent.worker_report is not None
                and self.context_dashboard is not None
            ):
                await self._publish_worker_context(parent, agent)
        else:
            parent.handle_child_failure(
                child_id=agent.session_id,
                failure_reason=agent.error_message or "Unknown failure",
                budget_at_failure=agent.current_budget,
            )

        for event in parent.events:
            await self.event_store.append(event, expected_version=parent_version)
            parent_version += 1
            self._notify_progress(event, parent)

        parent.mark_changes_as_committed()

    async def _recover_stuck_parent(self, parent: AgentSession) -> None:
        """Recover a WAITING parent by checking for missing child completions.

        This handles the case where a child completed but the parent notification
        was lost (e.g., due to process crash or network issue).
        """
        parent_version = parent.version
        any_updates = False

        for child_id in parent.child_ids:
            # Skip if we already recorded this child
            if child_id in parent.child_results or child_id in parent.child_failures:
                continue

            # Check child's current state
            child_events = await self.event_store.get_events(child_id)
            if not child_events:
                continue

            child = AgentSession.load_from_history(child_events)

            # If child is in terminal state but parent doesn't know, process it
            if child.status == AgentStatus.COMPLETED:
                parent.handle_child_update(
                    child_id,
                    child.result or "",
                    worker_report=child.worker_report,
                )
                any_updates = True
            elif child.status == AgentStatus.FAILED:
                parent.handle_child_failure(
                    child_id=child_id,
                    failure_reason=child.error_message or "Unknown failure",
                    budget_at_failure=child.current_budget,
                )
                any_updates = True

        # Persist any updates
        if any_updates:
            for event in parent.events:
                await self.event_store.append(event, expected_version=parent_version)
                parent_version += 1
                self._notify_progress(event, parent)
            parent.mark_changes_as_committed()

    async def _get_active_agent_ids(self, root_id: UUID | None = None) -> list[UUID]:
        """Return agent IDs not in terminal state (COMPLETED/FAILED).

        Args:
            root_id: If provided, only return agents within this hierarchy.
                    If None, returns all active agents (legacy behavior).
        """
        if root_id is not None:
            # Get only agents in this hierarchy
            collector = HierarchyCollector(self.event_store)
            all_agent_ids = await collector.collect_agent_ids(root_id)
        else:
            # Legacy: get all agents
            all_agent_ids = set(await self.event_store.get_all_aggregate_ids())

        active_ids = []
        for agent_id in all_agent_ids:
            events = await self.event_store.get_events(agent_id)
            if events:
                agent = AgentSession.load_from_history(events)
                if not agent.is_terminal():
                    active_ids.append(agent_id)
        return active_ids

    async def _get_agents_status(self, root_id: UUID | None = None) -> list[dict[str, Any]]:
        """Return status information for agents.

        Args:
            root_id: If provided, only return agents within this hierarchy.
                    If None, returns all agents (legacy behavior).
        """
        if root_id is not None:
            collector = HierarchyCollector(self.event_store)
            all_agent_ids = await collector.collect_agent_ids(root_id)
        else:
            all_agent_ids = set(await self.event_store.get_all_aggregate_ids())

        agents_status = []
        for agent_id in all_agent_ids:
            events = await self.event_store.get_events(agent_id)
            if events:
                agent = AgentSession.load_from_history(events)
                agents_status.append({
                    "agent_id": str(agent_id)[:8],
                    "role": agent.role.value,
                    "status": agent.status.value,
                    "budget": agent.current_budget,
                    "task": (agent.task_description[:40] + "...")
                    if agent.task_description and len(agent.task_description) > 40
                    else agent.task_description,
                    "queue_size": len(agent.task_queue),
                })
        return agents_status

    def _notify_status(self, agents_status: list[dict[str, Any]]) -> None:
        """Notify status callback if configured."""
        if self.status_callback is not None:
            with contextlib.suppress(Exception):
                self.status_callback(agents_status)

    async def run_system_loop(self, root_agent_id: UUID) -> None:
        """Poll and execute active agents in the hierarchy until all reach terminal state.

        Only processes agents within the hierarchy rooted at root_agent_id.
        This ensures that running a new task doesn't process agents from previous runs.

        Workers are executed in left-to-right tree order to ensure deterministic workspace
        modifications. The QueryService guarantees left-to-right execution through:

        1. Hierarchical Path Computation - Each agent gets a tuple path from root (e.g.,
           (0, 0, 1) means root's first child's second child). Paths sort lexicographically
           in left-to-right order.

        2. Left Sibling Blocking Check - Before a worker can execute, we verify no
           incomplete work exists to its left by walking up the ancestor chain and
           checking if any left siblings (and their subtrees) are incomplete.

        3. Sequential Worker Filtering - With sequential_workers=True, only the leftmost
           eligible worker is returned. Non-workers (PENDING, BOSS, MANAGER) can execute
           concurrently as they only perform reasoning/decomposition.

        Result: Only one worker executes at a time (the leftmost eligible), while managers
        decompose in parallel. This balances sequential correctness for work execution
        with parallel speed for task decomposition.
        """
        while True:
            # Get active agents with sequential worker filtering
            # This uses QueryService which properly checks left sibling completion
            active_agents = await self._query_service.get_active_agent_ids(
                root_id=root_agent_id,
                sequential_workers=True,  # Workers execute one at a time in left-to-right tree order
            )

            if not active_agents:
                break

            # Notify status callback with current agents status (filtered to hierarchy)
            if self.status_callback:
                agents_status = await self._query_service.get_agents_status(root_id=root_agent_id)
                self._notify_status(agents_status)

            # Separate workers from non-workers for different execution strategies
            non_worker_ids = []
            worker_id = None

            for agent_id in active_agents:
                events = await self.event_store.get_events(agent_id)
                if events:
                    agent = AgentSession.load_from_history(events)
                    if agent.role == AgentRole.WORKER:
                        worker_id = agent_id  # At most one worker due to QueryService filtering
                    else:
                        non_worker_ids.append(agent_id)

            # Execute non-workers CONCURRENTLY (managers decompose in parallel)
            # They only perform reasoning/decomposition, don't modify workspace
            if non_worker_ids:
                async def safe_run_step(aid: UUID) -> None:
                    try:
                        await self.run_agent_step(aid)
                    except Exception as e:
                        print(f"Error executing non-worker {aid}: {e!r}")

                await asyncio.gather(*[safe_run_step(aid) for aid in non_worker_ids])

            # Execute the single worker SEQUENTIALLY (one at a time, left-to-right)
            # Workers modify workspace, so they must execute in order
            if worker_id is not None:
                try:
                    await self.run_agent_step(worker_id)
                except Exception as e:
                    print(f"Error executing worker {worker_id}: {e!r}")

            await asyncio.sleep(self.poll_interval)

    async def get_agent_result(self, agent_id: UUID) -> AgentResultDTO:
        events = await self.event_store.get_events(agent_id)
        if not events:
            raise ValueError(f"Agent {agent_id} not found")

        agent = AgentSession.load_from_history(events)
        return AgentResultDTO(
            agent_id=str(agent.session_id),
            status=agent.status.value,
            result=agent.result,
            task_description=agent.task_description or "",
            role=agent.role.value,
        )

    async def get_system_statistics(self, root_agent_id: UUID) -> SystemStatisticsDTO:
        """Get statistics for agents in a specific run hierarchy.

        Args:
            root_agent_id: The root BOSS agent ID to scope statistics to.

        Returns:
            Statistics for only the agents in this run's hierarchy.
        """
        collector = HierarchyCollector(self.event_store)
        hierarchy_agent_ids = await collector.collect_agent_ids(root_agent_id)

        completed = 0
        failed = 0
        active = 0

        for agent_id in hierarchy_agent_ids:
            events = await self.event_store.get_events(agent_id)
            if events:
                agent = AgentSession.load_from_history(events)
                if agent.status == AgentStatus.COMPLETED:
                    completed += 1
                elif agent.status == AgentStatus.FAILED:
                    failed += 1
                else:
                    active += 1

        return SystemStatisticsDTO(
            total_agents=len(hierarchy_agent_ids),
            completed=completed,
            failed=failed,
            active=active,
        )

    async def initialize(self) -> None:
        await self.event_store.connect()
        await self.event_store.initialize_schema()
        # Initialize context dashboard if enabled
        if self.context_dashboard is not None:
            await self.context_dashboard.connect()
            await self.context_dashboard.initialize_schema()

    async def cleanup(self) -> None:
        await self.event_store.disconnect()
        # Cleanup context dashboard if enabled
        if self.context_dashboard is not None:
            await self.context_dashboard.disconnect()

    async def create_boss_agent(self, task_description: str) -> UUID:
        """Create root BOSS agent with task. Returns agent UUID."""
        root_id = uuid4()

        self._workspace_context_cache = None
        self._workspace_context_scanned = False

        # Use .resolve() for absolute path - worker tools may run in sandboxed environments
        if self.output_directory:
            base_output = Path(self.output_directory).resolve()
            base_output.mkdir(parents=True, exist_ok=True)
            run_output_path = base_output / str(root_id)
            run_output_path.mkdir(parents=True, exist_ok=True)
            self.working_directory = str(run_output_path)

        boss_model = self.model_config.get("boss", "o3-mini")
        boss_config = {
            "strategy": "heuristic",
            "base": {
                "model": boss_model,
                "temperature": 0.7,
                "max_tokens": 4000,  # Increased for detailed subtask decomposition with budget justifications
            },
            "tool": "claude_code",
        }

        boss_agent = AgentSession.create(
            session_id=root_id,
            role=AgentRole.BOSS,
            config=boss_config,
            parent_id=None,
        )
        boss_agent.assign_task(task_description)

        # Allocate initial budget to BOSS agent (default: 1000.0)
        initial_budget = 1000.0
        self.initial_boss_budget = initial_budget
        boss_agent.allocate_budget(initial_budget, source="initial")

        # Extract and publish source context from the original prompt
        # This runs before persisting so we can include any generated events
        await self._extract_and_publish_source_context(boss_agent, task_description)

        for idx, event in enumerate(boss_agent.events):
            await self.event_store.append(event, expected_version=idx)
            self._notify_progress(event, boss_agent)

        boss_agent.mark_changes_as_committed()
        return root_id
