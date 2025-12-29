"""Domain service for building hierarchical prompts from Jinja2 templates."""

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

if TYPE_CHECKING:
    from core.domain.subtask import SubtaskJustification


# Keywords that indicate a security-related task
SECURITY_KEYWORDS = frozenset({
    "cve",
    "cwe",
    "vulnerability",
    "exploit",
    "poc",
    "proof-of-concept",
    "proof of concept",
    "sanitizer",
    "addresssanitizer",
    "asan",
    "ubsan",
    "msan",
    "security benchmark",
    "security patch",
    "heap overflow",
    "buffer overflow",
    "use-after-free",
    "null pointer",
    "integer overflow",
    "memory corruption",
    "nvd",
    "cvss",
})


def is_security_task(task_description: str) -> bool:
    """Detect if a task is security-related based on keywords.

    Args:
        task_description: The task description to analyze.

    Returns:
        True if the task appears to be security-related.
    """
    lower_desc = task_description.lower()
    return any(keyword in lower_desc for keyword in SECURITY_KEYWORDS)


class PromptBuilder:
    """Compose hierarchical prompts: system → strategy → task → output format."""

    def __init__(
        self,
        template_dir: str | Path = "prompts",
        default_tool: str = "claude_code",
    ) -> None:
        self.template_dir = Path(template_dir)
        self.default_tool = default_tool
        # Autoescape disabled: templates generate LLM prompts (plain text), not HTML
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=False,  # noqa: S701
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def build_complexity_evaluation_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
        justification: "SubtaskJustification | None" = None,
    ) -> str:
        """Build prompt for PENDING agent complexity evaluation."""
        try:
            system = self.env.get_template("system/role_pending.j2").render()
            strategy = self.env.get_template("strategies/complexity_evaluation.j2").render()
            task = self.env.get_template("tasks/complexity_evaluation.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                parent_task=parent_task,
                justification=justification,
            )
            output_format = self.env.get_template("output_formats/complexity_result.j2").render()
            return f"{system}\n\n{strategy}\n\n{task}\n\n{output_format}"
        except TemplateNotFound as e:
            msg = f"Required template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def build_manager_decomposition_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: str = "MANAGER",
        parent_task: str | None = None,
        justification: "SubtaskJustification | None" = None,
    ) -> str:
        """Build prompt for MANAGER agent task decomposition."""
        try:
            system = self.env.get_template("system/role_manager.j2").render(
                default_tool=self.default_tool,
            )
            strategy = self.env.get_template("strategies/manager_decomposition.j2").render(
                default_tool=self.default_tool,
            )
            task = self.env.get_template("tasks/task_decomposition.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                agent_role=agent_role,
                parent_task=parent_task,
                default_tool=self.default_tool,
                justification=justification,
            )
            output_format = self.env.get_template("output_formats/subtask_list.j2").render(
                default_tool=self.default_tool,
            )
            return f"{system}\n\n{strategy}\n\n{task}\n\n{output_format}"
        except TemplateNotFound as e:
            msg = f"Required template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def build_boss_delegation_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
        justification: "SubtaskJustification | None" = None,
    ) -> str:
        """Build prompt for BOSS agent task delegation."""
        try:
            system = self.env.get_template("system/role_boss.j2").render(
                default_tool=self.default_tool,
            )
            strategy = self.env.get_template("strategies/boss_delegation.j2").render(
                default_tool=self.default_tool,
            )
            task = self.env.get_template("tasks/task_decomposition.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                agent_role="BOSS",
                parent_task=parent_task,
                default_tool=self.default_tool,
                justification=justification,
            )
            output_format = self.env.get_template("output_formats/subtask_list.j2").render(
                default_tool=self.default_tool,
            )
            return f"{system}\n\n{strategy}\n\n{task}\n\n{output_format}"
        except TemplateNotFound as e:
            msg = f"Required template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def build_security_benchmark_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
        justification: "SubtaskJustification | None" = None,
    ) -> str:
        """Build prompt for BOSS agent security benchmark generation.

        Uses security-specific templates that provide CVE analysis guidance,
        PoC generation strategies, and patch generation patterns.

        Args:
            task_description: The security task (should contain CVE details).
            agent_id: The agent's UUID.
            parent_task: Parent task context (usually None for BOSS).
            justification: Supervisor's justification for this subtask.

        Returns:
            Composed prompt string for security benchmark generation.
        """
        try:
            system = self.env.get_template("security/role_boss_security.j2").render(
                default_tool=self.default_tool,
            )
            strategy = self.env.get_template("security/strategy_cve_benchmark.j2").render(
                default_tool=self.default_tool,
            )
            task = self.env.get_template("tasks/task_decomposition.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                agent_role="BOSS",
                parent_task=parent_task,
                default_tool=self.default_tool,
                justification=justification,
            )
            output_format = self.env.get_template("security/output_format_benchmark.j2").render(
                default_tool=self.default_tool,
            )
            return f"{system}\n\n{strategy}\n\n{task}\n\n{output_format}"
        except TemplateNotFound as e:
            msg = f"Required security template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def build_security_worker_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        task_type: str = "poc",
    ) -> str:
        """Build prompt for WORKER agent security task execution.

        Args:
            task_description: The specific security task.
            agent_id: The agent's UUID.
            task_type: Type of security task ('poc' or 'patch').

        Returns:
            Composed prompt string for security worker.
        """
        template_map = {
            "poc": "security/worker_poc_generation.j2",
            "patch": "security/worker_patch_generation.j2",
        }
        template_name = template_map.get(task_type, "security/worker_poc_generation.j2")

        try:
            guidance = self.env.get_template(template_name).render()
            worker_role = self.env.get_template("system/role_worker.j2").render()
            return f"{worker_role}\n\n{guidance}\n\nYour task:\n{task_description}"
        except TemplateNotFound as e:
            msg = f"Required security worker template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def build_security_manager_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        parent_task: str | None = None,
        justification: "SubtaskJustification | None" = None,
    ) -> str:
        """Build prompt for MANAGER agent handling security subtasks.

        Args:
            task_description: The security subtask to decompose.
            agent_id: The agent's UUID.
            parent_task: Parent task context.
            justification: Supervisor's justification for this subtask.

        Returns:
            Composed prompt string for security manager.
        """
        try:
            system = self.env.get_template("system/role_manager.j2").render(
                default_tool=self.default_tool,
            )
            security_strategy = self.env.get_template("security/manager_security.j2").render(
                default_tool=self.default_tool,
            )
            task = self.env.get_template("tasks/task_decomposition.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                agent_role="MANAGER",
                parent_task=parent_task,
                default_tool=self.default_tool,
                justification=justification,
            )
            output_format = self.env.get_template("output_formats/subtask_list.j2").render(
                default_tool=self.default_tool,
            )
            return f"{system}\n\n{security_strategy}\n\n{task}\n\n{output_format}"
        except TemplateNotFound as e:
            msg = f"Required security manager template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def build_auto_prompt(
        self,
        task_description: str,
        agent_id: UUID,
        agent_role: str,
        parent_task: str | None = None,
        justification: "SubtaskJustification | None" = None,
    ) -> str:
        """Automatically select appropriate prompt based on task content.

        Detects security tasks and routes to security-specific prompts.

        Args:
            task_description: The task description.
            agent_id: The agent's UUID.
            agent_role: The agent's role (BOSS, MANAGER, etc.).
            parent_task: Parent task context.
            justification: Supervisor's justification for this subtask.

        Returns:
            Composed prompt string using appropriate templates.
        """
        is_security = is_security_task(task_description)

        if agent_role == "BOSS" and is_security:
            return self.build_security_benchmark_prompt(
                task_description=task_description,
                agent_id=agent_id,
                parent_task=parent_task,
                justification=justification,
            )
        if agent_role == "BOSS":
            return self.build_boss_delegation_prompt(
                task_description=task_description,
                agent_id=agent_id,
                parent_task=parent_task,
                justification=justification,
            )
        if agent_role == "MANAGER" and is_security:
            return self.build_security_manager_prompt(
                task_description=task_description,
                agent_id=agent_id,
                parent_task=parent_task,
                justification=justification,
            )
        if agent_role == "MANAGER":
            return self.build_manager_decomposition_prompt(
                task_description=task_description,
                agent_id=agent_id,
                agent_role=agent_role,
                parent_task=parent_task,
                justification=justification,
            )
        # Default to complexity evaluation for PENDING
        return self.build_complexity_evaluation_prompt(
            task_description=task_description,
            agent_id=agent_id,
            parent_task=parent_task,
            justification=justification,
        )

    def build_context_relevance_prompt(
        self,
        task_description: str,
        available_titles: list[tuple[UUID, str]],
        justification: "SubtaskJustification | None" = None,
        max_entries: int = 5,
    ) -> str:
        """Build prompt for LLM to evaluate context relevance.

        Args:
            task_description: The task the worker will execute.
            available_titles: List of (entry_id, work_title) tuples from context dashboard.
            justification: Supervisor's justification for this task.
            max_entries: Maximum number of relevant entries to return.

        Returns:
            Prompt string for context relevance evaluation.
        """
        try:
            template = self.env.get_template("context/relevance_query.j2")
            return template.render(
                task_description=task_description,
                available_titles=available_titles,
                justification=justification,
                max_entries=max_entries,
            )
        except TemplateNotFound as e:
            msg = f"Required template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def build_context_key_prompt(
        self,
        task_description: str,
        objective: str,
        deliverables: str,
        approach: str,
    ) -> str:
        """Build prompt for generating a concise context dashboard key.

        Args:
            task_description: The original task description.
            objective: What the task aimed to achieve.
            deliverables: What was produced.
            approach: How it was accomplished.

        Returns:
            Prompt string for key generation.
        """
        try:
            template = self.env.get_template("context/generate_key.j2")
            return template.render(
                task_description=task_description,
                objective=objective,
                deliverables=deliverables,
                approach=approach,
            )
        except TemplateNotFound as e:
            msg = f"Required template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def build_context_analysis_prompt(
        self,
        task_description: str,
        objective: str,
        justification: str,
        approach: str,
        reasoning: str,
        deliverables: str,
        challenges: str,
        observations: str = "",
        fulfillment_evidence: str = "",
    ) -> str:
        """Build prompt for synthesizing comprehensive work analysis.

        Args:
            task_description: The original task description.
            objective: What the task aimed to achieve.
            justification: Why this task was assigned.
            approach: How it was accomplished.
            reasoning: Reasoning behind decisions.
            deliverables: What was produced.
            challenges: Challenges encountered.
            observations: Key observations and discoveries during execution.
            fulfillment_evidence: Evidence of fulfilling supervisor's expectations.

        Returns:
            Prompt string for comprehensive analysis synthesis.
        """
        try:
            template = self.env.get_template("context/synthesize_analysis.j2")
            return template.render(
                task_description=task_description,
                objective=objective,
                justification=justification,
                approach=approach,
                reasoning=reasoning,
                deliverables=deliverables,
                challenges=challenges,
                observations=observations or "Task executed as planned",
                fulfillment_evidence=fulfillment_evidence or "Task completed successfully",
            )
        except TemplateNotFound as e:
            msg = f"Required template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def build_source_context_extraction_prompt(
        self,
        task_description: str,
    ) -> str:
        """Build prompt for extracting key information from Boss's original prompt.

        This prompt instructs the LLM to extract key references, bug reports,
        file paths, commit references, error messages, and other critical
        information from the user's original task description.

        Args:
            task_description: The original Boss prompt from the user.

        Returns:
            Prompt string for source context extraction.
        """
        try:
            template = self.env.get_template("context/extract_source_context.j2")
            return template.render(
                task_description=task_description,
            )
        except TemplateNotFound as e:
            msg = f"Required template not found: {e.name}"
            raise TemplateNotFound(msg) from e

    def get_cwe_guidance_for_worker(
        self,
        inferred_cwes: list[str],
        task_type: str = "patch",
    ) -> str:
        """Get CWE-specific guidance patterns for worker based on inferred CWEs.

        Extracts relevant sections from security templates based on the CWE types
        identified in the bug report. This provides workers with targeted fix
        patterns or exploitation strategies.

        Args:
            inferred_cwes: List of inferred CWE IDs (e.g., ['CWE-787', 'CWE-125']).
            task_type: Type of task ('patch' for fix patterns, 'poc' for exploitation).

        Returns:
            String containing relevant CWE-specific guidance from security templates.
        """
        if not inferred_cwes:
            return ""

        # CWE to guidance mapping - extracted from security templates
        # This provides targeted patterns based on inferred vulnerability types
        cwe_patterns: dict[str, dict[str, str]] = {
            # Memory Safety - Buffer Overflows
            "CWE-787": {
                "patch": (
                    "### CWE-787 (Out-of-bounds Write / Heap Buffer Overflow)\n"
                    "**Fix Pattern:** Add bounds checking before memory operations\n"
                    "```c\n"
                    "// BEFORE (vulnerable)\n"
                    "memcpy(dst, src, len);\n\n"
                    "// AFTER (fixed)\n"
                    "if (len <= dst_size) {\n"
                    "    memcpy(dst, src, len);\n"
                    "} else {\n"
                    "    return -1;  // or handle error\n"
                    "}\n"
                    "```\n"
                    "**Key Checks:**\n"
                    "- Validate `len` against destination buffer size\n"
                    "- Check for integer overflow in size calculations\n"
                    "- Use safe string functions (strlcpy, snprintf)\n"
                ),
                "poc": (
                    "### CWE-787 Exploitation Strategy\n"
                    "1. Find buffer allocation size\n"
                    "2. Craft input with size > buffer capacity\n"
                    "3. Trigger memcpy/strcpy with oversized input\n"
                    "**Expected:** `ERROR: AddressSanitizer: heap-buffer-overflow`\n"
                ),
            },
            "CWE-122": {
                "patch": (
                    "### CWE-122 (Heap-based Buffer Overflow)\n"
                    "**Fix Pattern:** Validate allocation and copy sizes\n"
                    "```c\n"
                    "// Ensure allocated size matches usage\n"
                    "size_t alloc_size = calculate_safe_size(input_len);\n"
                    "if (alloc_size == 0 || alloc_size < input_len) {\n"
                    "    return ERROR_OVERFLOW;\n"
                    "}\n"
                    "char *buf = malloc(alloc_size);\n"
                    "```\n"
                ),
                "poc": (
                    "### CWE-122 Exploitation Strategy\n"
                    "1. Identify heap allocation size\n"
                    "2. Provide input exceeding allocated bounds\n"
                    "**Expected:** `heap-buffer-overflow on address 0x...`\n"
                ),
            },
            # Out-of-bounds Read
            "CWE-125": {
                "patch": (
                    "### CWE-125 (Out-of-bounds Read)\n"
                    "**Fix Pattern:** Validate indices before array access\n"
                    "```c\n"
                    "// BEFORE (vulnerable)\n"
                    "return array[index];\n\n"
                    "// AFTER (fixed)\n"
                    "if (index >= 0 && index < array_size) {\n"
                    "    return array[index];\n"
                    "}\n"
                    "return default_value;  // or error\n"
                    "```\n"
                    "**Key Checks:**\n"
                    "- Validate array index before access\n"
                    "- Check string length before substring operations\n"
                ),
                "poc": (
                    "### CWE-125 Exploitation Strategy\n"
                    "1. Find array bounds\n"
                    "2. Provide index outside valid range\n"
                    "**Expected:** `ERROR: AddressSanitizer: heap-buffer-overflow READ`\n"
                ),
            },
            # Use-After-Free
            "CWE-416": {
                "patch": (
                    "### CWE-416 (Use After Free)\n"
                    "**Fix Pattern:** Nullify pointers after free, restructure ownership\n"
                    "```c\n"
                    "// BEFORE (vulnerable)\n"
                    "free(ptr);\n"
                    "// ... later ...\n"
                    "use(ptr);  // UAF!\n\n"
                    "// AFTER (fixed) - Option 1: Nullify\n"
                    "free(ptr);\n"
                    "ptr = NULL;\n\n"
                    "// AFTER (fixed) - Option 2: Reference counting\n"
                    "if (--ref_count == 0) {\n"
                    "    free(ptr);\n"
                    "    ptr = NULL;\n"
                    "}\n"
                    "```\n"
                    "**Key Patterns:**\n"
                    "- Set pointer to NULL immediately after free\n"
                    "- Use reference counting for shared resources\n"
                    "- Consider smart pointers in C++\n"
                ),
                "poc": (
                    "### CWE-416 Exploitation Strategy\n"
                    "1. Trigger object deallocation\n"
                    "2. Allocate new object of same size (heap feng shui)\n"
                    "3. Trigger use of dangling pointer\n"
                    "**Expected:** `ERROR: AddressSanitizer: heap-use-after-free`\n"
                ),
            },
            # Double Free
            "CWE-415": {
                "patch": (
                    "### CWE-415 (Double Free)\n"
                    "**Fix Pattern:** Track allocation state, nullify after free\n"
                    "```c\n"
                    "// BEFORE (vulnerable)\n"
                    "free(ptr);\n"
                    "// ... error path ...\n"
                    "free(ptr);  // Double free!\n\n"
                    "// AFTER (fixed)\n"
                    "if (ptr != NULL) {\n"
                    "    free(ptr);\n"
                    "    ptr = NULL;\n"
                    "}\n"
                    "```\n"
                ),
                "poc": (
                    "### CWE-415 Exploitation Strategy\n"
                    "1. Find conditional free paths\n"
                    "2. Trigger both paths to execute\n"
                    "**Expected:** `ERROR: AddressSanitizer: double-free`\n"
                ),
            },
            # NULL Pointer Dereference
            "CWE-476": {
                "patch": (
                    "### CWE-476 (NULL Pointer Dereference)\n"
                    "**Fix Pattern:** Add NULL checks before dereference\n"
                    "```c\n"
                    "// BEFORE (vulnerable)\n"
                    "result = ptr->field;\n\n"
                    "// AFTER (fixed)\n"
                    "if (ptr == NULL) {\n"
                    "    return ERROR_NULL;\n"
                    "}\n"
                    "result = ptr->field;\n"
                    "```\n"
                ),
                "poc": (
                    "### CWE-476 Exploitation Strategy\n"
                    "1. Find allocation that can return NULL\n"
                    "2. Trigger allocation failure (OOM, error condition)\n"
                    "3. Cause dereference of NULL pointer\n"
                    "**Expected:** `SEGV on unknown address 0x000000000000`\n"
                ),
            },
            # Integer Overflow
            "CWE-190": {
                "patch": (
                    "### CWE-190 (Integer Overflow)\n"
                    "**Fix Pattern:** Use safe arithmetic or wider types\n"
                    "```c\n"
                    "// BEFORE (vulnerable)\n"
                    "size_t total = count * sizeof(element);  // Can overflow!\n\n"
                    "// AFTER (fixed) - Safe multiplication\n"
                    "if (count > SIZE_MAX / sizeof(element)) {\n"
                    "    return ERROR_OVERFLOW;\n"
                    "}\n"
                    "size_t total = count * sizeof(element);\n"
                    "```\n"
                    "**Key Patterns:**\n"
                    "- Check before arithmetic, not after\n"
                    "- Use __builtin_mul_overflow() on GCC/Clang\n"
                    "- Use wider intermediate types\n"
                ),
                "poc": (
                    "### CWE-190 Exploitation Strategy\n"
                    "1. Find multiplication/addition used for allocation\n"
                    "2. Provide values that wrap around (e.g., SIZE_MAX / N + 1)\n"
                    "3. Result: small allocation, large copy\n"
                    "**Expected:** Subsequent buffer overflow from undersized allocation\n"
                ),
            },
            # Format String
            "CWE-134": {
                "patch": (
                    "### CWE-134 (Format String Vulnerability)\n"
                    "**Fix Pattern:** Never use user input as format string\n"
                    "```c\n"
                    "// BEFORE (vulnerable)\n"
                    "printf(user_input);  // DANGEROUS!\n\n"
                    "// AFTER (fixed)\n"
                    "printf(\"%s\", user_input);\n"
                    "// or\n"
                    "fputs(user_input, stdout);\n"
                    "```\n"
                ),
                "poc": (
                    "### CWE-134 Exploitation Strategy\n"
                    "1. Find printf-family function with user-controlled format\n"
                    "2. Provide format specifiers: `%s%s%s%s%s` or `%n`\n"
                    "**Expected:** Crash or memory corruption from format specifiers\n"
                ),
            },
            # Race Condition
            "CWE-362": {
                "patch": (
                    "### CWE-362 (Race Condition / TOCTOU)\n"
                    "**Fix Pattern:** Use atomic operations or proper locking\n"
                    "```c\n"
                    "// BEFORE (vulnerable TOCTOU)\n"
                    "if (access(path, R_OK) == 0) {\n"
                    "    fd = open(path, O_RDONLY);  // Race window!\n"
                    "}\n\n"
                    "// AFTER (fixed) - Open then check\n"
                    "fd = open(path, O_RDONLY);\n"
                    "if (fd < 0) {\n"
                    "    // Handle error\n"
                    "}\n"
                    "```\n"
                ),
                "poc": (
                    "### CWE-362 Exploitation Strategy\n"
                    "1. Identify check-then-use pattern\n"
                    "2. Race between check and use (symlink swap, etc.)\n"
                    "**Expected:** ThreadSanitizer warnings or unexpected behavior\n"
                ),
            },
            # Uninitialized Memory
            "CWE-457": {
                "patch": (
                    "### CWE-457 (Use of Uninitialized Variable)\n"
                    "**Fix Pattern:** Always initialize variables at declaration\n"
                    "```c\n"
                    "// BEFORE (vulnerable)\n"
                    "int result;\n"
                    "if (condition) result = compute();\n"
                    "return result;  // May be uninitialized!\n\n"
                    "// AFTER (fixed)\n"
                    "int result = DEFAULT_VALUE;\n"
                    "if (condition) result = compute();\n"
                    "return result;\n"
                    "```\n"
                ),
                "poc": (
                    "### CWE-457 Exploitation Strategy\n"
                    "1. Find variable used without guaranteed initialization\n"
                    "2. Skip the initialization path\n"
                    "**Expected:** MemorySanitizer: use-of-uninitialized-value\n"
                ),
            },
            # Command Injection
            "CWE-78": {
                "patch": (
                    "### CWE-78 (OS Command Injection)\n"
                    "**Fix Pattern:** Never use shell with user input, use execve\n"
                    "```c\n"
                    "// BEFORE (vulnerable)\n"
                    "char cmd[256];\n"
                    "snprintf(cmd, sizeof(cmd), \"ls %s\", user_path);\n"
                    "system(cmd);  // DANGEROUS!\n\n"
                    "// AFTER (fixed) - Use exec directly\n"
                    "char *argv[] = {\"ls\", sanitized_path, NULL};\n"
                    "execvp(\"ls\", argv);\n"
                    "```\n"
                ),
                "poc": (
                    "### CWE-78 Exploitation Strategy\n"
                    "1. Find system()/popen() with user input\n"
                    "2. Inject shell metacharacters: `; | && $()`\n"
                    "**Expected:** Arbitrary command execution\n"
                ),
            },
        }

        lines = [
            "## 🔧 CWE-SPECIFIC FIX PATTERNS FROM SECURITY KNOWLEDGE BASE",
            "",
            "Based on the inferred CWE patterns, here are targeted fix strategies:",
            "",
        ]

        for cwe in inferred_cwes:
            # Normalize CWE format (CWE-XXX)
            cwe_normalized = cwe.upper()
            if not cwe_normalized.startswith("CWE-"):
                cwe_normalized = f"CWE-{cwe_normalized}"

            if cwe_normalized in cwe_patterns:
                pattern = cwe_patterns[cwe_normalized].get(task_type, "")
                if pattern:
                    lines.append(pattern)
                    lines.append("")
            else:
                # Provide generic guidance for unknown CWEs
                lines.append(f"### {cwe_normalized}")
                lines.append(f"Refer to https://cwe.mitre.org/data/definitions/{cwe_normalized.replace('CWE-', '')}.html")
                lines.append("")

        lines.append("---")
        lines.append("")

        return "\n".join(lines)
