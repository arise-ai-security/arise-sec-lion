"""Prompt management API routes.

Provides endpoints for viewing and editing prompt templates.
Uses async file I/O to prevent blocking the event loop.
"""

import asyncio
from pathlib import Path

import aiofiles
import aiofiles.os
from fastapi import APIRouter, HTTPException
from jinja2 import Environment, TemplateSyntaxError

from query.api.schemas import (
    PromptListSchema,
    PromptSchema,
    PromptUpdateSchema,
    PromptVariablesSchema,
    RenderedPromptSchema,
)


router = APIRouter()

# Base prompts directory
PROMPTS_DIR = Path("prompts")

# Valid categories
VALID_CATEGORIES = {"system", "strategies", "tasks", "output_formats"}

# Available template variables with descriptions
TEMPLATE_VARIABLES = {
    "default_tool": "Default worker tool (claude_code or openhands)",
    "task_description": "The task assigned to the agent",
    "agent_id": "UUID of the agent",
    "agent_role": "Role of the agent (BOSS, MANAGER, WORKER, PENDING)",
    "parent_task": "Description of the parent agent's task (if any)",
}

# Sample values for preview rendering
SAMPLE_VARIABLES = {
    "default_tool": "claude_code",
    "task_description": "Create a hello world Python script",
    "agent_id": "abc12345-6789-0def-ghij-klmnopqrstuv",
    "agent_role": "MANAGER",
    "parent_task": "Build a web application",
}


def get_prompt_path(category: str, name: str) -> Path:
    """Get the file path for a prompt template."""
    return PROMPTS_DIR / category / f"{name}.j2"


def validate_category(category: str) -> None:
    """Validate that the category is valid."""
    if category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category '{category}'. Valid: {', '.join(VALID_CATEGORIES)}",
        )


async def _path_exists(path: Path) -> bool:
    """Check if path exists asynchronously."""
    return await aiofiles.os.path.exists(path)


async def _read_file(path: Path) -> str:
    """Read file content asynchronously."""
    async with aiofiles.open(path, encoding="utf-8") as f:
        return await f.read()


async def _write_file(path: Path, content: str) -> None:
    """Write content to file asynchronously."""
    async with aiofiles.open(path, mode="w", encoding="utf-8") as f:
        await f.write(content)


async def _glob_templates(directory: Path, pattern: str) -> list[Path]:
    """Glob for files asynchronously using executor.

    pathlib.glob is synchronous, so we run it in the default executor.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: list(directory.glob(pattern)))


@router.get("", response_model=PromptListSchema)
async def list_all_prompts() -> PromptListSchema:
    """List all prompt templates across all categories."""
    prompts = []

    for category in VALID_CATEGORIES:
        category_dir = PROMPTS_DIR / category
        if await _path_exists(category_dir):
            template_files = await _glob_templates(category_dir, "*.j2")
            for template_file in template_files:
                name = template_file.stem
                content = await _read_file(template_file)

                prompts.append(
                    PromptSchema(
                        category=category,
                        name=name,
                        content=content,
                        updated_at=None,
                    )
                )

    return PromptListSchema(prompts=prompts, total=len(prompts))


@router.get("/variables", response_model=PromptVariablesSchema)
async def get_template_variables() -> PromptVariablesSchema:
    """Get available Jinja2 template variables and their descriptions."""
    return PromptVariablesSchema(variables=TEMPLATE_VARIABLES)


@router.get("/{category}", response_model=PromptListSchema)
async def list_category_prompts(category: str) -> PromptListSchema:
    """List all prompt templates in a specific category."""
    validate_category(category)

    prompts = []
    category_dir = PROMPTS_DIR / category

    if await _path_exists(category_dir):
        template_files = await _glob_templates(category_dir, "*.j2")
        for template_file in template_files:
            name = template_file.stem
            content = await _read_file(template_file)

            prompts.append(
                PromptSchema(
                    category=category,
                    name=name,
                    content=content,
                    updated_at=None,
                )
            )

    return PromptListSchema(prompts=prompts, total=len(prompts))


@router.get("/{category}/{name}", response_model=PromptSchema)
async def get_prompt(category: str, name: str) -> PromptSchema:
    """Get a single prompt template by category and name."""
    validate_category(category)

    path = get_prompt_path(category, name)
    if not await _path_exists(path):
        raise HTTPException(status_code=404, detail=f"Prompt '{category}/{name}' not found")

    content = await _read_file(path)

    return PromptSchema(
        category=category,
        name=name,
        content=content,
        updated_at=None,
    )


@router.put("/{category}/{name}", response_model=PromptSchema)
async def update_prompt(category: str, name: str, update: PromptUpdateSchema) -> PromptSchema:
    """Update a prompt template.

    Validates Jinja2 syntax before saving.
    """
    validate_category(category)

    path = get_prompt_path(category, name)
    if not await _path_exists(path):
        raise HTTPException(status_code=404, detail=f"Prompt '{category}/{name}' not found")

    # Validate Jinja2 syntax
    try:
        env = Environment(autoescape=False)  # noqa: S701
        env.parse(update.content)
    except TemplateSyntaxError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Jinja2 syntax: {e.message} (line {e.lineno})",
        ) from e

    # Save the updated content asynchronously
    await _write_file(path, update.content)

    return PromptSchema(
        category=category,
        name=name,
        content=update.content,
        updated_at=None,
    )


@router.post("/{category}/{name}/preview", response_model=RenderedPromptSchema)
async def preview_prompt(category: str, name: str) -> RenderedPromptSchema:
    """Preview a prompt template rendered with sample variables."""
    validate_category(category)

    path = get_prompt_path(category, name)
    if not await _path_exists(path):
        raise HTTPException(status_code=404, detail=f"Prompt '{category}/{name}' not found")

    content = await _read_file(path)

    # Render with sample variables
    try:
        env = Environment(autoescape=False)  # noqa: S701
        template = env.from_string(content)
        rendered = template.render(**SAMPLE_VARIABLES)

        # Find which variables are used
        ast = env.parse(content)
        from jinja2 import meta

        variables_used = list(meta.find_undeclared_variables(ast))

    except TemplateSyntaxError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Jinja2 syntax: {e.message}",
        ) from e

    return RenderedPromptSchema(
        rendered=rendered,
        variables_used=variables_used,
    )


@router.post("/{category}/{name}/reset", response_model=PromptSchema)
async def reset_prompt(category: str, name: str) -> PromptSchema:
    """Reset a prompt to its default (file-based) content.

    Note: In the current file-based implementation, this is a no-op.
    When database storage is implemented, this will restore from the
    original file content.
    """
    validate_category(category)

    path = get_prompt_path(category, name)
    if not await _path_exists(path):
        raise HTTPException(status_code=404, detail=f"Prompt '{category}/{name}' not found")

    content = await _read_file(path)

    return PromptSchema(
        category=category,
        name=name,
        content=content,
        updated_at=None,
    )
