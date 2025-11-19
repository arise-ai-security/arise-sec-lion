"""Utility for loading and rendering Jinja2 prompt templates.

This module provides a simple interface for loading prompts from the prompts/
directory and rendering them with context variables.
"""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape


# Get the project root (two levels up from this file)
PROJECT_ROOT = Path(__file__).parent.parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"


def get_jinja_env() -> Environment:
    """Get configured Jinja2 environment for prompt templates.

    Returns:
        Configured Jinja2 Environment with FileSystemLoader.
    """
    return Environment(
        loader=FileSystemLoader(PROMPTS_DIR),
        autoescape=select_autoescape(),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_prompt(template_path: str, **context: Any) -> str:
    """Load and render a Jinja2 prompt template.

    Args:
        template_path: Relative path to template from prompts/ directory.
                      Example: "manager/task_decomposition.j2"
        **context: Variables to pass to the template for rendering.

    Returns:
        Rendered prompt string.

    Example:
        >>> prompt = render_prompt(
        ...     "manager/task_decomposition.j2",
        ...     task_description="Build a web scraper"
        ... )
    """
    env = get_jinja_env()
    template = env.get_template(template_path)
    return template.render(**context)
