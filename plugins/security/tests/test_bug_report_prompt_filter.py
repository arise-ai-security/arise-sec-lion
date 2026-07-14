"""Tests for answer-bearing bug-report prompt filtering."""

from core.application.services import PromptParser
from core.domain.values.prompt_trace import SectionProvenance
from plugins.security.cve_instance import CVEInstance
from plugins.security.plugin import SecurityDomainPlugin


def test_template_context_excludes_bug_report_but_preserves_failure_oracles() -> None:
    """The solver context omits the answer-bearing report without losing safe metadata."""

    # Given: A CVE record containing distinct prompt-safe and answer-bearing sentinels
    bug_report = "BUG_REPORT_ANSWER_KEY_SENTINEL"
    bug_description = "BUG_DESCRIPTION_PROMPT_SAFE_SENTINEL"
    sanitizer_report = "SANITIZER_REPORT_PROMPT_SAFE_SENTINEL"
    cve = CVEInstance(
        instance_id="example.cve-2026-0001",
        repo="example/project",
        project_name="example",
        lang="C",
        work_dir="/src/example",
        sanitizer="address",
        bug_description=bug_description,
        base_commit="a" * 40,
        sanitizer_report=sanitizer_report,
        bug_report=bug_report,
    )

    # When: The CVE record is serialized for prompt templates
    context = cve.to_template_context()

    # Then: The answer-bearing report remains on the record but not in the prompt context
    assert cve.bug_report == bug_report
    assert cve.model_dump()["bug_report"] == bug_report
    assert "bug_report" not in context
    assert bug_report not in context.values()

    # And: Prompt-safe failure context is preserved unchanged
    assert context["bug_description"] == bug_description
    assert context["sanitizer_report"] == sanitizer_report


def test_security_plugin_classifies_bug_report_tags() -> None:
    plugin = SecurityDomainPlugin()
    parser = PromptParser(
        extra_tag_mappings=plugin.get_tag_mappings(),
        extra_provenance_patterns=plugin.get_provenance_patterns(),
    )

    sections = parser.parse("<bug_report_details>content</bug_report_details>")

    assert sections[0].provenance == SectionProvenance.SYSTEM
