"""Domain service for building hierarchical prompts from Jinja2 templates."""

from pathlib import Path
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from core.domain.enums import AgentRole


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
    ) -> str:
        """Build prompt for PENDING agent complexity evaluation."""
        try:
            system = self.env.get_template("system/role_pending.j2").render()
            strategy = self.env.get_template("strategies/complexity_evaluation.j2").render()
            task = self.env.get_template("tasks/complexity_evaluation.j2").render(
                task_description=task_description,
                agent_id=str(agent_id),
                parent_task=parent_task,
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
        agent_role: AgentRole = AgentRole.MANAGER,
        parent_task: str | None = None,
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
                agent_role=agent_role.value.upper(),
                parent_task=parent_task,
                default_tool=self.default_tool,
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
                agent_role=AgentRole.BOSS.value.upper(),
                parent_task=parent_task,
                default_tool=self.default_tool,
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
    ) -> str:
        """Build prompt for BOSS agent security benchmark generation.

        Uses security-specific templates that provide CVE analysis guidance,
        PoC generation strategies, and patch generation patterns.

        Args:
            task_description: The security task (should contain CVE details).
            agent_id: The agent's UUID.
            parent_task: Parent task context (usually None for BOSS).

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
                agent_role=AgentRole.BOSS.value.upper(),
                parent_task=parent_task,
                default_tool=self.default_tool,
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
    ) -> str:
        """Build prompt for MANAGER agent handling security subtasks.

        Args:
            task_description: The security subtask to decompose.
            agent_id: The agent's UUID.
            parent_task: Parent task context.

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
                agent_role=AgentRole.MANAGER.value.upper(),
                parent_task=parent_task,
                default_tool=self.default_tool,
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
        agent_role: AgentRole,
        parent_task: str | None = None,
    ) -> str:
        """Automatically select appropriate prompt based on task content.

        Detects security tasks and routes to security-specific prompts.

        Args:
            task_description: The task description.
            agent_id: The agent's UUID.
            agent_role: The agent's role (BOSS, MANAGER, etc.).
            parent_task: Parent task context.

        Returns:
            Composed prompt string using appropriate templates.
        """
        is_security = is_security_task(task_description)

        if agent_role == AgentRole.BOSS and is_security:
            return self.build_security_benchmark_prompt(
                task_description=task_description,
                agent_id=agent_id,
                parent_task=parent_task,
            )
        if agent_role == AgentRole.BOSS:
            return self.build_boss_delegation_prompt(
                task_description=task_description,
                agent_id=agent_id,
                parent_task=parent_task,
            )
        if agent_role == AgentRole.MANAGER and is_security:
            return self.build_security_manager_prompt(
                task_description=task_description,
                agent_id=agent_id,
                parent_task=parent_task,
            )
        if agent_role == AgentRole.MANAGER:
            return self.build_manager_decomposition_prompt(
                task_description=task_description,
                agent_id=agent_id,
                agent_role=agent_role,
                parent_task=parent_task,
            )
        # Default to complexity evaluation for PENDING
        return self.build_complexity_evaluation_prompt(
            task_description=task_description,
            agent_id=agent_id,
            parent_task=parent_task,
        )
