"""Placement of the shared code-prefix block in the worker stable prefix.

The block (``context/shared_code.j2``) must render inside the worker's STABLE
PREFIX — after the CVE context and before the BEF-role-specific section — so it
lands in the identical-across-workers region and is OpenAI prefix-cacheable. When
no block is supplied the worker prompt must be byte-identical to today.
"""

from pathlib import Path
from uuid import uuid4

from core.application.services import PromptBuilder
from plugins.security import CVEInstance, SecBenchPromptStrategy


def _prompts_dir() -> Path:
    local_path = Path(__file__).resolve().parents[3] / "prompts"
    return local_path if local_path.exists() else Path("prompts")


def _cve() -> CVEInstance:
    return CVEInstance(
        instance_id="demo.cve-2024-0001",
        repo="demo/project",
        project_name="demo",
        lang="c",
        work_dir="/src/demo",
        sanitizer="address",
        bug_description="Heap overflow in demo parser.",
        base_commit="a" * 40,
    )


def _builder() -> PromptBuilder:
    return PromptBuilder(
        template_dir=_prompts_dir(),
        default_tool="openhands",
        strategy=SecBenchPromptStrategy(),
    )


_SENTINEL_PATH = "/src/demo/vuln.c"
_SENTINEL_CONTENT = "int vulnerable_fn() { /* SENTINEL-CODE-BODY */ return 0; }"
_SHARED_BLOCK = (
    "<provided_source_files>\n"
    f'<file path="{_SENTINEL_PATH}">\n{_SENTINEL_CONTENT}\n</file>\n'
    "</provided_source_files>"
)

# The BEF-role-specific section is the worker/<branch>.j2 partial, which opens
# with this tag (e.g. fixer.j2 / exploiter.j2 / builder.j2).
_ROLE_SECTION_MARKER = "<domain_instructions>"
# The CVE context (cve.j2) opens the <domain> block.
_CVE_MARKER = "<cve_instance>"


def test_shared_block_renders_inside_stable_prefix_before_role_section() -> None:
    # Given: a fixer worker prompt with a shared code block supplied
    builder = _builder()

    # When
    prompt = builder.build_worker_prompt(
        task_description="[fixer] Patch the heap overflow in demo parser.",
        agent_id=uuid4(),
        domain_context=_cve(),
        shared_code_block=_SHARED_BLOCK,
    )

    # Then: the block is present, AFTER the CVE context and BEFORE the role section.
    # Anchor on the unique sentinel content (the rendered file body) so the prose
    # mention of ``<provided_source_files>`` in worker.j2 cannot confuse positions.
    assert _SENTINEL_CONTENT in prompt
    cve_idx = prompt.index(_CVE_MARKER)
    block_idx = prompt.index(_SENTINEL_CONTENT)
    role_idx = prompt.index(_ROLE_SECTION_MARKER)
    assert cve_idx < block_idx < role_idx


def test_no_shared_block_is_byte_identical_to_today() -> None:
    # Given: the same worker prompt built with and without the block (None)
    builder = _builder()
    task = "[exploiter] Reproduce the crash with a PoC."
    agent_id = uuid4()

    # When
    without = builder.build_worker_prompt(
        task_description=task,
        agent_id=agent_id,
        domain_context=_cve(),
    )
    with_none = builder.build_worker_prompt(
        task_description=task,
        agent_id=agent_id,
        domain_context=_cve(),
        shared_code_block=None,
    )

    # Then: passing shared_code_block=None changes nothing — byte-identical.
    # (The worker.j2 mindset mentions ``<provided_source_files>`` in prose, so
    # guard on the rendered block's opening, which only appears when injected.)
    assert without == with_none
    assert "<provided_source_files>\n<file" not in without


def test_shared_block_in_stable_prefix_identical_across_two_roles() -> None:
    # Given: two different-role workers on the SAME CVE with the SAME block
    builder = _builder()
    block = _SHARED_BLOCK

    fixer = builder.build_worker_prompt(
        task_description="[fixer] Patch the overflow.",
        agent_id=uuid4(),
        domain_context=_cve(),
        shared_code_block=block,
    )
    exploiter = builder.build_worker_prompt(
        task_description="[exploiter] Reproduce the overflow.",
        agent_id=uuid4(),
        domain_context=_cve(),
        shared_code_block=block,
    )

    # Then: the prefix up to the role-section divergence is byte-identical, and
    # the shared block sits within that identical region.
    fixer_prefix = fixer[: fixer.index(_ROLE_SECTION_MARKER)]
    exploiter_prefix = exploiter[: exploiter.index(_ROLE_SECTION_MARKER)]
    assert fixer_prefix == exploiter_prefix
    assert _SENTINEL_CONTENT in fixer_prefix
