"""Task 14: Optimize role/operation prompts via GPT-5.4-pro as a meta-optimizer.

Implements OpenAI's Prompt-Optimizer-equivalent workflow via the PUBLIC
``/v1/responses`` API. The internal dashboard endpoint
(``/v1/dashapi/optimize/promptv2``) is browser-session-gated and Cloudflare-
bound to the original browser's IP + TLS fingerprint, so it can't be called
from automation. We get functionally-equivalent output by instructing
gpt-5.4-pro to apply the same optimization principles (clarity, concision,
structure, specificity, contract preservation) under strict Jinja2-
placeholder preservation rules.

Targets (tree-arm prompts only; the flat CLI baseline uses Claude Code's
built-in system prompt, untouched):

    prompts/system.j2
    prompts/roles/{boss,manager,worker,pending}.j2
    prompts/operations/{assess,decomposition,execution}.j2

Pre-opt copies archived to ``experiments/role_prompts/pre_opt/`` before any
writes; post-opt archived to ``experiments/role_prompts/post_opt/`` after
successful optimization.

Usage:
    uv run python scripts/optimize_role_prompts.py [--model gpt-5.4-pro] [--dry-run] [--only <rel-path>]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "prompts"
PRE_OPT_DIR = REPO_ROOT / "experiments" / "role_prompts" / "pre_opt"
POST_OPT_DIR = REPO_ROOT / "experiments" / "role_prompts" / "post_opt"


SYSTEM_CONTEXT = (
    "This is a Jinja2 template in a recursive multi-agent cybersecurity analysis "
    "system (BOSS -> MANAGER -> WORKER tree) that reproduces and patches CVEs "
    "against the SEC-bench benchmark. Workers run autonomously inside Docker "
    "containers with Valgrind + KLEE. PRESERVE EVERY Jinja placeholder -- "
    "`{{ var }}`, `{% if %}`, `{% for %}`, comments `{# ... #}`. Do not rename "
    "variables. Do not remove conditional branches."
)


META_PROMPT = """\
You are an expert prompt engineer for GPT-5-class large language models,
acting as OpenAI's official Prompt Optimizer tool would. You rewrite prompts
to maximize their effectiveness while preserving every structural invariant
of the original.

APPLY THESE OPTIMIZATION PRINCIPLES (mirroring OpenAI's Prompt Optimizer):

1. CLARITY: Remove ambiguity. Make each instruction directly actionable.
   Replace abstract verbs with concrete ones ("analyze" -> "read and
   identify", "investigate" -> "read file X, grep for Y"). Favor imperative
   voice.

2. CONCISION: Eliminate redundancy, filler words, and duplicated guidance
   while preserving every concrete directive and every domain-specific
   detail. Compress, do not over-simplify. Never drop a concrete example
   or a specific warning (e.g., "do NOT scan entire projects; they time
   out").

3. STRUCTURE: Use clean hierarchical structure (markdown headings, bullets,
   XML-like tags where the original uses them) that GPT-5 parses
   reliably. Group related instructions. Use numbered lists for ordered
   steps, bullets for unordered lists.

4. SPECIFICITY: Replace vague goals with concrete actions, success
   criteria, and examples wherever the domain permits. Keep the domain's
   technical terminology (CVE, sanitizer, Valgrind, KLEE, repro, patch,
   /testcase/, etc.).

5. CONTRACT PRESERVATION: Do not remove or alter any stated output-format
   requirement (JSON contracts, specific field names, enum values,
   "Pure JSON only" rules, "return ONLY..." instructions). Preserve exact
   key names, expected types, and example output blocks.

STRICT PRESERVATION RULES -- violations are failures:

- Preserve every Jinja2 construct EXACTLY as it appears in the input:
  - `{{ variable_name }}` and `{{ obj.attr }}` and `{{ var | filter }}`
  - `{% if ... %}` / `{% elif ... %}` / `{% else %}` / `{% endif %}`
  - `{% for x in xs %}` / `{% endfor %}`
  - `{# ... #}` comment blocks
  - Whitespace-control variants like `{%- if x -%}`
- Preserve every conditional branch. Do NOT collapse `{% if A %}X{% else %}Y{% endif %}`
  into one path even if the bodies look similar. The branches exist for
  a reason (feature flags, constraint signalling) and downstream code
  depends on them rendering differently.
- Preserve named XML-like tags if the original uses them
  (`<persona>`, `<operation>`, `<briefing>`, `<output_format>`,
  `<system>`, `<task>`, `<thinker_justification>`, `<workspace>`, etc.).
  Maintain the same opening/closing tag pairs; do not rename them.
- Preserve exact JSON output-format specifications verbatim, including
  field names, example blocks, numbered "Rules:" enumerations, and any
  "if constraints cannot be satisfied" error-output specifications.
- Do NOT introduce new Jinja variables that did not exist in the input.
- Do NOT add YAML front-matter, markdown code fences around the whole
  output, preambles like "Here is the optimized prompt:", postambles
  like "Let me know if you want changes", or any commentary.

OUTPUT: Return ONLY the optimized prompt text, verbatim ready to write to
the target .j2 file. First character of your response must be the first
character of the optimized prompt. Last character must be the last
character of the optimized prompt (no trailing whitespace, no trailing
commentary).
"""


@dataclass(frozen=True)
class PromptTarget:
    relative_path: str
    meta: str


TARGETS: list[PromptTarget] = [
    PromptTarget(
        relative_path="system.j2",
        meta=(
            f"{SYSTEM_CONTEXT}\n\n"
            "Use case: Shared system-prompt header prepended to every LLM call across "
            "all four agent roles (BOSS, MANAGER, WORKER, PENDING).\n"
            "Desired behaviour: Very short, role-agnostic preamble that establishes "
            "the agent is part of a tree hierarchy. Conditional branch: BOSS/MANAGER "
            "get a <config_reference> block about configured defaults; WORKER/PENDING "
            "get a minimal one-liner.\n"
            "Issues: Currently wordy for a header. Should be maximally terse. Do not "
            "expand into role-specific guidance -- that lives in the role templates."
        ),
    ),
    PromptTarget(
        relative_path="roles/boss.j2",
        meta=(
            f"{SYSTEM_CONTEXT}\n\n"
            "Use case: Persona for the BOSS (root) agent. Runs at the start of every "
            "task. Establishes BOSS as the analysis-first coordinator that decomposes "
            "cybersecurity tasks into 2-3 autonomous phase subtasks (typically "
            "Builder -> Exploiter -> Fixer) and delegates to MANAGER children. BOSS "
            "never executes directly.\n"
            "Desired behaviour: Emphasize (1) analysis before decomposition; (2) rich "
            "justification objects that transfer domain expertise to children, since "
            "children are fully autonomous and cannot ask for clarification; (3) tool "
            "prescriptions workers should install (Valgrind, KLEE, debuggers); (4) "
            "context-sharing expectations via <context-update> tags so downstream "
            "siblings get upstream findings.\n"
            "Issues: Tends to produce vague descriptions like 'analyze the "
            "vulnerability' instead of concrete commands. Should push toward specific "
            "file paths, CVE IDs, and actionable deliverables. Do NOT add JSON output "
            "format here -- that is handled by a separate operation template."
        ),
    ),
    PromptTarget(
        relative_path="roles/manager.j2",
        meta=(
            f"{SYSTEM_CONTEXT}\n\n"
            "Use case: Persona for MANAGER (mid-level) agents. Receives a phase-level "
            "task from BOSS, investigates via recon tools (read_file, search_codebase, "
            "get_symbols_overview), then decomposes into worker-executable subtasks. "
            "Never executes directly.\n"
            "Desired behaviour: Emphasize (1) research before decomposition using "
            "recon tools; (2) worker-executable descriptions with exact commands and "
            "file paths (workers use smaller models with limited context); (3) "
            "peer-observation instructions -- workers must scrutinize upstream sibling "
            "outputs, not blindly trust them; (4) depends_on for sequential ordering; "
            "(5) <context-update> publishing contracts per worker.\n"
            "Issues: Tends to under-specify worker tasks. Optimize for precision and "
            "compactness in worker-facing instructions. Do NOT add JSON output format."
        ),
    ),
    PromptTarget(
        relative_path="roles/worker.j2",
        meta=(
            f"{SYSTEM_CONTEXT}\n\n"
            "Use case: Persona for WORKER (leaf) agents. Runs inside a Docker "
            "container with terminal + file-editor tools; executes shell commands, "
            "edits source code, installs apt packages, runs Valgrind/KLEE, writes "
            "artifacts to /testcase/. Has a hard iteration budget.\n"
            "Desired behaviour: Emphasize (1) check-before-doing: `ls /testcase/` "
            "first, read existing artifacts, skip redundant work from previous "
            "sequential workers; (2) target-specific-files-not-whole-projects -- "
            "full-project tool scans time out, must pass specific file paths; (3) "
            "iteration efficiency -- plan before acting, combine related commands; "
            "(4) descriptive artifact naming -- never output.txt, always "
            "tool_target_analysis_type.txt; (5) report results with evidence; (6) "
            "share discoveries via <context-update> for downstream siblings.\n"
            "Issues: Workers currently run whole-project scans and time out, or "
            "produce generic artifact names. Longest template in the system; preserve "
            "every concrete directive -- compression must not drop the scan-specific-"
            "files warning or the artifact-naming rules."
        ),
    ),
    PromptTarget(
        relative_path="roles/pending.j2",
        meta=(
            f"{SYSTEM_CONTEXT}\n\n"
            "Use case: Short persona for PENDING agents -- newly-created agents in "
            "the 'undecided' state. A single LLM call classifies them as SIMPLE "
            "(become WORKER, execute directly) or COMPLEX (become MANAGER, "
            "decompose). The default is EXECUTE: most tasks should execute directly.\n"
            "Desired behaviour: Concise decision framework with strong bias toward "
            "EXECUTE. Only recommend DECOMPOSE when the task genuinely spans 3+ "
            "independent subsystems. Emphasize the bias -- over-decomposition "
            "inflates cost in our experiment.\n"
            "Issues: Already short. Keep it short. Do NOT add JSON format (that's in "
            "operations/assess.j2)."
        ),
    ),
    PromptTarget(
        relative_path="operations/assess.j2",
        meta=(
            f"{SYSTEM_CONTEXT}\n\n"
            "Use case: Operation-level prompt for the PENDING assess-task decision. "
            "Rendered into the LLM call that outputs the `execute | decompose | "
            "constraints_unsatisfiable` classification. Used with optional recon "
            "tool-calling.\n"
            "Desired behaviour: Preserve the STRICT JSON contract exactly: the LLM "
            "must return ONLY valid JSON with `{\"action\": \"execute\" | "
            "\"decompose\", \"reasoning\": \"...\"}` (with subtasks array when "
            "decomposing, or `{\"status\": \"constraints_unsatisfiable\", ...}` when "
            "limits are hit). Preserve every depth/agent constraint branch "
            "(at_max_depth, agents_remaining, depth_remaining). Preserve the recon-"
            "tool-calling branch (has_tools). Preserve the briefing, ancestry, and "
            "thinker_justification sections.\n"
            "Issues: LLMs occasionally emit Markdown fences around the JSON -- the "
            "rules explicitly say 'Pure JSON only, no markdown' and that must stay "
            "prominent. Optimize for JSON compliance first, clarity second."
        ),
    ),
    PromptTarget(
        relative_path="operations/decomposition.j2",
        meta=(
            f"{SYSTEM_CONTEXT}\n\n"
            "Use case: Operation-level prompt for BOSS/MANAGER decomposition. Outputs "
            "a JSON array of subtasks. Rendered into every MANAGER evaluate_task call "
            "and every BOSS evaluate_task call.\n"
            "Desired behaviour: Preserve the STRICT JSON-array output contract. "
            "Preserve every depth-aware branch (at_max_depth, depth_remaining tiers "
            "that change required granularity: module-level at depth 3+, file/function "
            "at depth 2, code-block/variable at depth 1). Preserve recon-tool "
            "availability branch. Preserve max_subtasks and agents_remaining "
            "constraint language. Preserve <briefing> and <thinker_justification> "
            "blocks. Descriptions must be self-contained and worker-executable with "
            "specific file paths and commands.\n"
            "Issues: Largest prompt in the system. LLMs sometimes produce JSON with "
            "comments or trailing commas. Optimize for strict JSON compliance. Do NOT "
            "lose target_paths / symbols / search_hints fields -- they drive recon "
            "precision in children."
        ),
    ),
    PromptTarget(
        relative_path="operations/execution.j2",
        meta=(
            f"{SYSTEM_CONTEXT}\n\n"
            "Use case: Operation-level prompt for WORKER execution. Rendered into the "
            "task sent to the Claude Agent SDK / OpenHands / Google ADK worker tool. "
            "Worker then runs autonomously with terminal + file editor until finish, "
            "timeout, or iteration-budget exhaustion.\n"
            "Desired behaviour: Preserve the 'absorb context first -> validate peer "
            "work -> plan -> execute -> verify -> share findings' workflow. Preserve "
            "the briefing, ancestry, decisions, thinker_justification, task, and "
            "workspace_context Jinja blocks. Emphasize that the worker CREATES real "
            "files (not code snippets) and VERIFIES end-to-end (file exists, commands "
            "succeed). Emphasize apt-get install -y for missing tools. Emphasize "
            "validating upstream-sibling outputs before building on them.\n"
            "Issues: Workers occasionally explain what they would do instead of doing "
            "it. Optimize for action-first phrasing. Do NOT drop the 'CREATE actual "
            "files -- do not just explain' rule."
        ),
    ),
]


def archive(paths: list[Path], destination_root: Path) -> None:
    for prompt_path in paths:
        rel = prompt_path.relative_to(PROMPTS_DIR)
        dest = destination_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(prompt_path, dest)
        logger.info("archived %s -> %s", prompt_path, dest)


def build_user_message(target: PromptTarget, current_content: str) -> str:
    return (
        "USE CASE / CONTEXT:\n"
        f"{target.meta}\n"
        "\n---\n\n"
        "PROMPT TO OPTIMIZE (verbatim; preserve every Jinja construct exactly):\n"
        "```jinja\n"
        f"{current_content}\n"
        "```\n"
    )


def strip_outer_code_fence(text: str) -> str:
    """Remove a single outer ```jinja ... ``` or ``` ... ``` fence if present."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        # Drop first line (fence + optional lang tag) and last fence
        lines = stripped.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1])
    return text


def call_responses_api(
    client: httpx.Client,
    model: str,
    meta_prompt: str,
    user_message: str,
    *,
    reasoning_effort: str = "medium",
) -> str:
    """Call /v1/responses with a reasoning model and extract the output text."""
    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "developer", "content": meta_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    # Pro / reasoning models accept a reasoning block
    if "pro" in model or "o1" in model or "o3" in model:
        payload["reasoning"] = {"effort": reasoning_effort}

    r = client.post("/v1/responses", json=payload, timeout=300.0)
    r.raise_for_status()
    data = r.json()

    # Walk output[] for the final message's output_text.
    output = data.get("output") or []
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                txt = content.get("text")
                if isinstance(txt, str):
                    chunks.append(txt)
    text = "\n".join(chunks).strip()
    if not text:
        # Fallback: some models return a top-level "output_text" string
        top = data.get("output_text")
        if isinstance(top, str):
            text = top.strip()
    if not text:
        raise RuntimeError(
            f"no output_text in response (model={model}). Full response head:\n"
            f"{str(data)[:2000]}"
        )
    return text


def validate_jinja(text: str, original: str) -> list[str]:
    """Detect obvious structural losses. Returns a list of warnings (empty = clean)."""
    warnings: list[str] = []

    # 1) Every `{{ var }}` placeholder in the original should appear in the output.
    orig_vars = set(re.findall(r"\{\{\s*[^}]+\s*\}\}", original))
    new_vars = set(re.findall(r"\{\{\s*[^}]+\s*\}\}", text))
    missing = orig_vars - new_vars
    if missing:
        warnings.append(f"Missing Jinja placeholders: {sorted(missing)}")

    # 2) Every {% if %} in the original -> at least as many in the output.
    for keyword in ("if", "for", "elif", "else", "endif", "endfor", "include", "extends"):
        orig_count = len(re.findall(rf"\{{%-?\s*{keyword}\b", original))
        new_count = len(re.findall(rf"\{{%-?\s*{keyword}\b", text))
        if new_count < orig_count:
            warnings.append(
                f"Jinja tag {{% {keyword} %}} count dropped: {orig_count} -> {new_count}"
            )

    # 3) If original had an <output_format> block, new text must too.
    for tag in ("<output_format>", "<briefing>", "<task>", "<persona>", "<operation>", "<system>"):
        if tag in original and tag not in text:
            warnings.append(f"Missing tag: {tag}")

    # 4) Suspicious leading commentary
    if re.match(r"^(Here\s+is|Below\s+is|The\s+optimized|Optimized:)", text.lstrip()):
        warnings.append("Output starts with commentary (likely 'Here is the...')")

    return warnings


def optimize_one(
    client: httpx.Client,
    target: PromptTarget,
    model: str,
    reasoning_effort: str,
) -> str:
    current = (PROMPTS_DIR / target.relative_path).read_text()
    user_msg = build_user_message(target, current)
    logger.info(
        "optimizing %s (prompt=%d chars, meta=%d chars, model=%s)",
        target.relative_path,
        len(current),
        len(target.meta),
        model,
    )
    text = call_responses_api(
        client,
        model=model,
        meta_prompt=META_PROMPT,
        user_message=user_msg,
        reasoning_effort=reasoning_effort,
    )
    text = strip_outer_code_fence(text)
    warnings = validate_jinja(text, current)
    if warnings:
        for w in warnings:
            logger.warning("[%s] %s", target.relative_path, w)
    else:
        logger.info("[%s] structural validation clean", target.relative_path)
    return text


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="gpt-5.4-pro")
    ap.add_argument("--reasoning-effort", default="medium", choices=["low", "medium", "high"])
    ap.add_argument("--only", default=None, help="relative path under prompts/ to optimize a single target")
    ap.add_argument("--dry-run", action="store_true", help="write outputs to experiments/role_prompts/dry_run/ instead of prompts/")
    ap.add_argument("--fail-on-warnings", action="store_true", help="exit non-zero if any structural warning fires")
    args = ap.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        # Attempt to source from deployment/.env
        env_file = REPO_ROOT / "deployment" / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("OPENAI_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip("'\"")
                    break
    if not api_key:
        logger.error(
            "OPENAI_API_KEY not set. Export it or put it in deployment/.env."
        )
        return 2

    targets = [t for t in TARGETS if args.only is None or t.relative_path == args.only]
    if not targets:
        logger.error("no targets match --only=%s", args.only)
        return 2

    prompt_paths = [PROMPTS_DIR / t.relative_path for t in targets]
    archive(prompt_paths, PRE_OPT_DIR)

    dest_root = (REPO_ROOT / "experiments" / "role_prompts" / "dry_run") if args.dry_run else None

    failures: list[tuple[str, str]] = []
    all_warnings: list[tuple[str, list[str]]] = []

    client = httpx.Client(
        base_url="https://api.openai.com",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        for target in targets:
            try:
                optimized = optimize_one(
                    client,
                    target,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                )
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:500] if exc.response is not None else ""
                failures.append((target.relative_path, f"HTTP {exc.response.status_code}: {body}"))
                logger.error("[%s] HTTP error: %s", target.relative_path, body)
                continue
            except Exception as exc:  # noqa: BLE001
                failures.append((target.relative_path, str(exc)))
                logger.exception("[%s] failed", target.relative_path)
                continue

            # Record warnings
            warnings = validate_jinja(optimized, (PROMPTS_DIR / target.relative_path).read_text())
            if warnings:
                all_warnings.append((target.relative_path, warnings))

            if dest_root is not None:
                out_path = dest_root / target.relative_path
            else:
                out_path = PROMPTS_DIR / target.relative_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(optimized if optimized.endswith("\n") else optimized + "\n")
            logger.info("wrote %d chars to %s", len(optimized), out_path)

            time.sleep(0.5)  # polite pacing between calls
    finally:
        client.close()

    if dest_root is None and not failures:
        archive(prompt_paths, POST_OPT_DIR)

    if failures:
        print("\nFailures:")
        for name, err in failures:
            print(f"  {name}: {err}")
        return 1

    if all_warnings:
        print("\nStructural warnings (inspect before trusting):")
        for name, warns in all_warnings:
            print(f"  {name}:")
            for w in warns:
                print(f"    - {w}")
        if args.fail_on_warnings:
            return 1

    print("\nAll targets optimized successfully.")
    if args.dry_run:
        print(f"Outputs in: {dest_root}")
    else:
        print("Pre-opt archived to: experiments/role_prompts/pre_opt/")
        print("Post-opt archived to: experiments/role_prompts/post_opt/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
