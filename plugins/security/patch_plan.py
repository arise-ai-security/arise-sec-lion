"""Validated, literal SEC-bench patch-plan execution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field


class PatchOperation(BaseModel):
    """One exact source transformation anchored to unique text."""

    model_config = {"frozen": True}

    kind: Literal["insert_before", "insert_after", "replace", "delete"]
    anchor: str = Field(min_length=1)
    expected_occurrences: int = Field(default=1, ge=1)
    replacement: str = ""


class PatchPlan(BaseModel):
    """Frozen manager-authored plan consumed literally by Patch-Applier."""

    model_config = {"frozen": True}

    schema_version: Literal["1"] = "1"
    evidence_references: tuple[str, ...] = Field(min_length=1)
    target_file: str
    target_symbol: str
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: tuple[PatchOperation, ...] = Field(min_length=1)
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    forbidden_paths: tuple[str, ...] = Field(min_length=1)
    required_postconditions: tuple[str, ...] = Field(min_length=1)
    pre_patch_exploit_identity: str = Field(min_length=1)
    validation_commands: tuple[str, ...] = Field(min_length=1)

    @property
    def sha256(self) -> str:
        payload = self.model_dump_json(exclude_none=False, by_alias=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PatchPlanValidation:
    """Pre-write validation result and transformed content."""

    approved: bool
    plan_sha256: str
    reason: str
    target_path: Path | None = None
    original_content: str | None = None
    patched_content: str | None = None


@dataclass(frozen=True, slots=True)
class PatchApplyResult:
    """Literal Patch-Applier outcome."""

    status: Literal["APPLIED", "PATCH_PLAN_BLOCKED"]
    plan_sha256: str
    reason: str


@dataclass(frozen=True, slots=True)
class PatchRenderResult:
    """Validated patch artifact rendered without mutating the live source tree."""

    status: Literal["RENDERED", "PATCH_PLAN_BLOCKED"]
    plan_sha256: str
    reason: str
    diff: str = ""


class PatchPlanValidator:
    """Validate a plan against the sealed source workspace before any edit."""

    def __init__(self, source_root: Path, evidence_references: set[str]) -> None:
        self._source_root = source_root.resolve()
        self._evidence_references = evidence_references

    def validate(self, plan: PatchPlan) -> PatchPlanValidation:
        target = self._resolve_source_path(plan.target_file)
        if target is None:
            return self._blocked(plan, "target path is outside /src")
        if not target.is_file():
            return self._blocked(plan, "target file does not exist")
        if not self._path_is_allowed(plan.target_file, plan.allowed_paths):
            return self._blocked(plan, "target path is not allowlisted")
        if self._path_is_allowed(plan.target_file, plan.forbidden_paths):
            return self._blocked(plan, "target path is forbidden")
        if PurePosixPath(plan.target_file) == PurePosixPath("/src/build.sh"):
            return self._blocked(plan, "build.sh cannot be modified by a security patch")
        missing = set(plan.evidence_references) - self._evidence_references
        if missing:
            return self._blocked(plan, f"unresolved evidence references: {sorted(missing)}")
        commands = set(plan.validation_commands)
        required_commands = {"secb patch", "secb build", "secb repro"}
        if commands != required_commands:
            return self._blocked(plan, "validation commands must be exactly secb patch/build/repro")

        original = target.read_text(encoding="utf-8")
        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_hash != plan.base_sha256:
            return self._blocked(plan, "target hash does not match plan base_sha256")

        patched = original
        for operation in plan.operations:
            occurrences = patched.count(operation.anchor)
            if occurrences != operation.expected_occurrences:
                return self._blocked(
                    plan,
                    f"anchor occurrence mismatch: expected {operation.expected_occurrences}, "
                    f"found {occurrences}",
                )
            patched = self._apply_operation(patched, operation)

        if patched == original:
            return self._blocked(plan, "plan produces no source change")
        return PatchPlanValidation(
            approved=True,
            plan_sha256=plan.sha256,
            reason="approved",
            target_path=target,
            original_content=original,
            patched_content=patched,
        )

    def _resolve_source_path(self, path: str) -> Path | None:
        posix = PurePosixPath(path)
        if not posix.is_absolute() or posix.parts[:2] != ("/", "src"):
            return None
        target = (self._source_root / Path(*posix.parts[2:])).resolve()
        return target if target.is_relative_to(self._source_root) else None

    @staticmethod
    def _path_is_allowed(path: str, prefixes: tuple[str, ...]) -> bool:
        target = PurePosixPath(path)
        return any(target == PurePosixPath(prefix) or target.is_relative_to(prefix) for prefix in prefixes)

    @staticmethod
    def _apply_operation(content: str, operation: PatchOperation) -> str:
        # Transform every matched site; validation already pinned the match count
        # to the plan's expected_occurrences, so this stays consistent with it.
        if operation.kind == "insert_before":
            return content.replace(operation.anchor, operation.replacement + operation.anchor)
        if operation.kind == "insert_after":
            return content.replace(operation.anchor, operation.anchor + operation.replacement)
        if operation.kind == "replace":
            return content.replace(operation.anchor, operation.replacement)
        return content.replace(operation.anchor, "")

    @staticmethod
    def _blocked(plan: PatchPlan, reason: str) -> PatchPlanValidation:
        return PatchPlanValidation(approved=False, plan_sha256=plan.sha256, reason=reason)


class PatchApplier:
    """Apply only a prevalidated plan; mismatch leaves the workspace untouched."""

    def __init__(self, validator: PatchPlanValidator) -> None:
        self._validator = validator

    def apply(self, plan: PatchPlan) -> PatchApplyResult:
        validation = self._validator.validate(plan)
        if not validation.approved:
            return PatchApplyResult(
                status="PATCH_PLAN_BLOCKED",
                plan_sha256=validation.plan_sha256,
                reason=validation.reason,
            )
        if validation.target_path is None or validation.patched_content is None:
            raise RuntimeError("approved PatchPlan validation omitted transformed source")
        validation.target_path.write_text(validation.patched_content, encoding="utf-8")
        return PatchApplyResult(
            status="APPLIED",
            plan_sha256=validation.plan_sha256,
            reason="plan applied literally",
        )

    def render(self, plan: PatchPlan) -> PatchRenderResult:
        """Render the literal edit as a unified diff without editing ``/src``."""
        validation = self._validator.validate(plan)
        if not validation.approved:
            return PatchRenderResult(
                status="PATCH_PLAN_BLOCKED",
                plan_sha256=validation.plan_sha256,
                reason=validation.reason,
            )
        if validation.original_content is None or validation.patched_content is None:
            raise RuntimeError("approved PatchPlan validation omitted source content")
        relative = PurePosixPath(plan.target_file).relative_to("/src").as_posix()
        body = "".join(
            unified_diff(
                validation.original_content.splitlines(keepends=True),
                validation.patched_content.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        diff = f"diff --git a/{relative} b/{relative}\n{body}"
        return PatchRenderResult(
            status="RENDERED",
            plan_sha256=validation.plan_sha256,
            reason="plan rendered literally",
            diff=diff,
        )
