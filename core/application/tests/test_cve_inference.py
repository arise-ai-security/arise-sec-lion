"""Unit tests for CVE Instance inference service."""

import pytest

from core.application.services.cve_inference import (
    CVEInferenceError,
    CVEInstanceInferenceService,
    ExtractionResult,
)


class TestExtractionResult:
    """Tests for ExtractionResult dataclass."""

    def test_has_required_fields_with_all_required(self) -> None:
        """Should return True when repo and 40-char commit are present."""
        result = ExtractionResult(
            cve_id="cve-2023-1234",
            repo="gpac/gpac",
            base_commit="a" * 40,
            sanitizer="address",
            project_name="gpac",
            lang="c",
        )
        assert result.has_required_fields() is True

    def test_has_required_fields_missing_repo(self) -> None:
        """Should return False when repo is missing."""
        result = ExtractionResult(
            cve_id="cve-2023-1234",
            repo=None,
            base_commit="a" * 40,
            sanitizer="address",
            project_name=None,
            lang=None,
        )
        assert result.has_required_fields() is False

    def test_has_required_fields_missing_commit(self) -> None:
        """Should return False when commit is missing."""
        result = ExtractionResult(
            cve_id="cve-2023-1234",
            repo="gpac/gpac",
            base_commit=None,
            sanitizer="address",
            project_name="gpac",
            lang=None,
        )
        assert result.has_required_fields() is False

    def test_has_required_fields_short_commit(self) -> None:
        """Should return False when commit is not 40 chars."""
        result = ExtractionResult(
            cve_id="cve-2023-1234",
            repo="gpac/gpac",
            base_commit="abc123",  # Only 6 chars
            sanitizer="address",
            project_name="gpac",
            lang=None,
        )
        assert result.has_required_fields() is False


class TestCVEInstanceInferenceService:
    """Tests for CVEInstanceInferenceService."""

    @pytest.fixture
    def service(self) -> CVEInstanceInferenceService:
        return CVEInstanceInferenceService()

    # --- Regex Extraction Tests ---

    def test_extract_cve_id(self, service: CVEInstanceInferenceService) -> None:
        """Should extract CVE ID from task description."""
        result = service.extract("Fix CVE-2023-2838 in gpac")
        assert result.cve_id == "cve-2023-2838"

    def test_extract_cve_id_case_insensitive(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should extract CVE ID regardless of case."""
        result = service.extract("Fix cve-2023-2838 in gpac")
        assert result.cve_id == "cve-2023-2838"

    def test_extract_commit_40_char(self, service: CVEInstanceInferenceService) -> None:
        """Should extract 40-char commit hash."""
        commit = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        result = service.extract(f"Fix at commit {commit}")
        assert result.base_commit == commit

    def test_extract_commit_ignores_short(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should ignore commit hashes shorter than 40 chars."""
        result = service.extract("Fix at commit abc123def")
        assert result.base_commit is None

    def test_extract_repo_from_github_url(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should extract repo from GitHub URL."""
        result = service.extract("Fix in https://github.com/gpac/gpac")
        assert result.repo == "gpac/gpac"
        assert result.project_name == "gpac"

    def test_extract_repo_from_github_url_with_trailing_slash(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should handle trailing slash in URL."""
        result = service.extract("Fix in https://github.com/gpac/gpac/")
        assert result.repo == "gpac/gpac"

    def test_extract_repo_from_git_protocol(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should extract repo from git@ URL."""
        result = service.extract("Clone git@github.com:gpac/gpac.git")
        assert result.repo == "gpac/gpac"

    def test_extract_sanitizer_address(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should detect AddressSanitizer keywords."""
        cases = [
            "AddressSanitizer error",
            "ASAN detected",
            "heap-buffer-overflow",
            "use-after-free",
        ]
        for text in cases:
            result = service.extract(text)
            assert result.sanitizer == "address", f"Failed for: {text}"

    def test_extract_sanitizer_memory(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should detect MemorySanitizer keywords."""
        result = service.extract("MemorySanitizer: uninitialized value")
        assert result.sanitizer == "memory"

    def test_extract_sanitizer_undefined(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should detect UndefinedBehaviorSanitizer keywords."""
        result = service.extract("runtime error: undefined behavior")
        assert result.sanitizer == "undefined"

    def test_extract_sanitizer_default(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should default to 'address' when no sanitizer detected."""
        result = service.extract("Fix a bug")
        assert result.sanitizer == "address"

    # --- Language Detection Tests ---

    def test_extract_lang_c(self, service: CVEInstanceInferenceService) -> None:
        """Should detect C language from keywords."""
        cases = ["Fix bug in main.c", "C program crash", "written in C"]
        for text in cases:
            result = service.extract(text)
            assert result.lang == "c", f"Failed for: {text}"

    def test_extract_lang_cpp(self, service: CVEInstanceInferenceService) -> None:
        """Should detect C++ language from keywords."""
        cases = ["Fix bug in main.cpp", "C++ code", "parser.cc crash"]
        for text in cases:
            result = service.extract(text)
            assert result.lang == "c++", f"Failed for: {text}"

    def test_extract_lang_python(self, service: CVEInstanceInferenceService) -> None:
        """Should detect Python language from keywords."""
        cases = ["Fix script.py", "python crash", "Python error"]
        for text in cases:
            result = service.extract(text)
            assert result.lang == "python", f"Failed for: {text}"

    def test_extract_lang_rust(self, service: CVEInstanceInferenceService) -> None:
        """Should detect Rust language from keywords."""
        result = service.extract("Fix bug in lib.rs")
        assert result.lang == "rust"

    def test_extract_lang_none(self, service: CVEInstanceInferenceService) -> None:
        """Should return None when no language detected."""
        result = service.extract("Fix a bug in the parser")
        assert result.lang is None

    # --- Full Task Description Tests ---

    def test_extract_full_description(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should extract all fields from a complete description."""
        task = (
            "Fix CVE-2023-2838 in https://github.com/gpac/gpac: "
            "heap-buffer-overflow at commit "
            "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        )
        result = service.extract(task)

        assert result.cve_id == "cve-2023-2838"
        assert result.repo == "gpac/gpac"
        assert result.base_commit == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        assert result.sanitizer == "address"
        assert result.project_name == "gpac"

    # --- infer_instance Tests ---

    def test_infer_instance_success(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should create CVEInstance when all required fields present."""
        task = (
            "Fix CVE-2023-2838 in https://github.com/gpac/gpac at "
            "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        )
        instance = service.infer_instance(task, fail_fast=False)

        assert instance is not None
        assert instance.instance_id == "gpac.cve-2023-2838"
        assert instance.repo == "gpac/gpac"
        assert instance.project_name == "gpac"
        assert instance.base_commit == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        assert instance.sanitizer == "address"
        assert instance.lang == ""  # Empty when not detected (won't render in prompt)
        assert instance.work_dir == "/src"

    def test_infer_instance_with_lang_detection(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should detect language from task description."""
        task = (
            "Fix CVE-2023-2838 in https://github.com/gpac/gpac C++ parser at "
            "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        )
        instance = service.infer_instance(task, fail_fast=False)

        assert instance is not None
        assert instance.lang == "c++"

    def test_infer_instance_fail_fast_missing_cve_id(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should raise CVEInferenceError when CVE ID is missing."""
        task = (
            "Fix bug in https://github.com/gpac/gpac at "
            "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        )

        with pytest.raises(CVEInferenceError) as exc_info:
            service.infer_instance(task, fail_fast=True)

        assert "cve_id" in str(exc_info.value).lower()

    def test_infer_instance_fail_fast_missing_repo(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should raise CVEInferenceError when repo is missing and fail_fast=True."""
        task = "Fix CVE-2023-1234 at commit " + "a" * 40

        with pytest.raises(CVEInferenceError) as exc_info:
            service.infer_instance(task, fail_fast=True)

        assert "repo" in str(exc_info.value).lower()
        assert "repo" in exc_info.value.missing_fields[0].lower()

    def test_infer_instance_fail_fast_missing_commit(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should raise CVEInferenceError when commit is missing and fail_fast=True."""
        task = "Fix CVE-2023-1234 in https://github.com/gpac/gpac"

        with pytest.raises(CVEInferenceError) as exc_info:
            service.infer_instance(task, fail_fast=True)

        assert "commit" in str(exc_info.value).lower()

    def test_infer_instance_no_fail_returns_none(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should return None when required fields missing and fail_fast=False."""
        task = "Fix some bug"
        instance = service.infer_instance(task, fail_fast=False)
        assert instance is None

    def test_infer_instance_bug_description_empty_when_inferred(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should have empty bug_description when inferred (no curated CVE data)."""
        task = (
            "Fix CVE-2023-2838 in https://github.com/gpac/gpac at "
            "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        )
        instance = service.infer_instance(task, fail_fast=False)

        assert instance is not None
        assert instance.bug_description == ""  # Empty when inferred

    def test_infer_instance_has_cve_data_false_for_inferred(
        self, service: CVEInstanceInferenceService
    ) -> None:
        """Should have has_cve_data=False for inferred instances (empty bug_description)."""
        task = (
            "Fix CVE-2023-1234 in https://github.com/gpac/gpac at "
            "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        )
        instance = service.infer_instance(task, fail_fast=False)

        assert instance is not None
        # has_cve_data is False because bug_description is empty when inferred
        assert instance.bug_description == ""
        assert instance.has_cve_data is False


class TestCVEInferenceError:
    """Tests for CVEInferenceError exception."""

    def test_error_message_includes_missing_fields(self) -> None:
        """Should include missing fields in error message."""
        error = CVEInferenceError(["repo", "base_commit"])
        assert "repo" in str(error)
        assert "base_commit" in str(error)

    def test_error_includes_hint(self) -> None:
        """Should include hint about --cve-file in error message."""
        error = CVEInferenceError(["repo"])
        assert "--cve-file" in str(error)
