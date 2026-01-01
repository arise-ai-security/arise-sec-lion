"""Test cases for the composable context system.

Tests the ContextComposer and new context data types:
- ParentSummary, AncestorData, AncestryChain
- SiblingResults, SiblingEntry
- SharedDecisions, SharedArtifacts
- CustomContext
"""

from uuid import uuid4

import pytest

from core.application.services.context_composer import ContextComposer
from core.domain.values.context import (
    AncestorData,
    AncestorEntry,
    AncestryChain,
    ArtifactEntry,
    CustomContext,
    DecisionEntry,
    ParentSummary,
    SharedArtifacts,
    SharedDecisions,
    SiblingEntry,
    SiblingResults,
)


class TestParentSummary:
    """Tests for ParentSummary data type."""

    def test_creation(self) -> None:
        """Test creating ParentSummary."""
        summary = ParentSummary(
            task="Analyze vulnerability",
            result="Found SQL injection",
            decisions=("use_parameterized_queries",),
            role="manager",
        )
        assert summary.task == "Analyze vulnerability"
        assert summary.result == "Found SQL injection"
        assert summary.decisions == ("use_parameterized_queries",)
        assert summary.role == "manager"

    def test_template_key(self) -> None:
        """Test template_key property."""
        summary = ParentSummary(task="Task")
        assert summary.template_key == "parent_summary"

    def test_to_template_dict(self) -> None:
        """Test to_template_dict serialization."""
        summary = ParentSummary(
            task="Test task",
            result="Test result",
            decisions=("d1", "d2"),
            role="worker",
        )
        d = summary.to_template_dict()
        assert d["task"] == "Test task"
        assert d["result"] == "Test result"
        assert d["decisions"] == ("d1", "d2")
        assert d["role"] == "worker"

    def test_immutability(self) -> None:
        """Test that ParentSummary is immutable."""
        summary = ParentSummary(task="Task", result="Result")
        with pytest.raises(Exception):  # Pydantic frozen model
            summary.task = "Modified"  # type: ignore


class TestAncestorData:
    """Tests for AncestorData data type."""

    def test_creation(self) -> None:
        """Test creating AncestorData with label."""
        agent_id = uuid4()
        ancestor = AncestorData(
            label="fixer",
            agent_id=agent_id,
            role="manager",
            task="Fix vulnerability",
            result="Patch applied",
            decisions=("validate_inputs",),
            depth=1,
        )
        assert ancestor.label == "fixer"
        assert ancestor.agent_id == agent_id
        assert ancestor.role == "manager"
        assert ancestor.depth == 1

    def test_template_key_includes_label(self) -> None:
        """Test template_key includes the label."""
        ancestor = AncestorData(
            label="boss_goal",
            agent_id=uuid4(),
            role="boss",
            task="Main goal",
        )
        assert ancestor.template_key == "ancestor_boss_goal"

    def test_to_template_dict(self) -> None:
        """Test to_template_dict serialization."""
        agent_id = uuid4()
        ancestor = AncestorData(
            label="test",
            agent_id=agent_id,
            role="manager",
            task="Task",
            result="Result",
            decisions=("d1",),
            depth=2,
        )
        d = ancestor.to_template_dict()
        assert d["label"] == "test"
        assert d["agent_id"] == agent_id
        assert d["role"] == "manager"
        assert d["depth"] == 2


class TestAncestryChain:
    """Tests for AncestryChain data type."""

    def test_creation(self) -> None:
        """Test creating AncestryChain with entries."""
        chain = AncestryChain(
            ancestors=(
                AncestorEntry(agent_id="1", role="boss", task_summary="Root", depth=0),
                AncestorEntry(agent_id="2", role="manager", task_summary="Sub", depth=1),
            )
        )
        assert len(chain.ancestors) == 2
        assert chain.ancestors[0].role == "boss"
        assert chain.ancestors[1].role == "manager"

    def test_template_key(self) -> None:
        """Test template_key property."""
        chain = AncestryChain(ancestors=())
        assert chain.template_key == "ancestry_chain"

    def test_to_template_dict_includes_depth(self) -> None:
        """Test to_template_dict includes computed depth."""
        chain = AncestryChain(
            ancestors=(
                AncestorEntry(agent_id="1", role="boss", task_summary="R", depth=0),
                AncestorEntry(agent_id="2", role="manager", task_summary="M", depth=1),
                AncestorEntry(agent_id="3", role="manager", task_summary="M2", depth=2),
            )
        )
        d = chain.to_template_dict()
        assert d["depth"] == 3
        assert len(d["ancestors"]) == 3


class TestSiblingResults:
    """Tests for SiblingResults data type."""

    def test_creation(self) -> None:
        """Test creating SiblingResults."""
        results = SiblingResults(
            siblings=(
                SiblingEntry(
                    agent_id="1",
                    index=0,
                    status="completed",
                    task_summary="Task 1",
                    result_summary="Done",
                ),
                SiblingEntry(
                    agent_id="2",
                    index=1,
                    status="in_progress",
                    task_summary="Task 2",
                ),
            )
        )
        assert len(results.siblings) == 2

    def test_template_key(self) -> None:
        """Test template_key property."""
        results = SiblingResults(siblings=())
        assert results.template_key == "sibling_results"

    def test_to_template_dict_includes_counts(self) -> None:
        """Test to_template_dict includes computed counts."""
        results = SiblingResults(
            siblings=(
                SiblingEntry(agent_id="1", index=0, status="completed", task_summary="T1"),
                SiblingEntry(agent_id="2", index=1, status="completed", task_summary="T2"),
                SiblingEntry(agent_id="3", index=2, status="in_progress", task_summary="T3"),
                SiblingEntry(agent_id="4", index=3, status="pending", task_summary="T4"),
            )
        )
        d = results.to_template_dict()
        assert d["total_count"] == 4
        assert d["completed_count"] == 2
        assert d["in_progress_count"] == 1  # "analyzing" and "in_progress"


class TestSharedDecisions:
    """Tests for SharedDecisions data type."""

    def test_creation(self) -> None:
        """Test creating SharedDecisions."""
        decisions = SharedDecisions(
            decisions=(
                DecisionEntry(key="framework", value="django", rationale="Required"),
                DecisionEntry(key="database", value="postgres"),
            )
        )
        assert len(decisions.decisions) == 2

    def test_template_key(self) -> None:
        """Test template_key property."""
        decisions = SharedDecisions(decisions=())
        assert decisions.template_key == "shared_decisions"


class TestSharedArtifacts:
    """Tests for SharedArtifacts data type."""

    def test_creation(self) -> None:
        """Test creating SharedArtifacts."""
        artifacts = SharedArtifacts(
            artifacts=(
                ArtifactEntry(
                    key="report",
                    content_type="text/markdown",
                    content="# Report",
                    stored_by="agent-1",
                ),
            )
        )
        assert len(artifacts.artifacts) == 1

    def test_template_key(self) -> None:
        """Test template_key property."""
        artifacts = SharedArtifacts(artifacts=())
        assert artifacts.template_key == "shared_artifacts"


class TestCustomContext:
    """Tests for CustomContext data type."""

    def test_creation(self) -> None:
        """Test creating CustomContext with arbitrary data."""
        ctx = CustomContext(
            label="vulnerability_info",
            data={
                "cve_id": "CVE-2023-1234",
                "severity": "critical",
                "affected": ["auth_module"],
            },
        )
        assert ctx.label == "vulnerability_info"
        assert ctx.data["cve_id"] == "CVE-2023-1234"

    def test_template_key_includes_label(self) -> None:
        """Test template_key includes custom_ prefix and label."""
        ctx = CustomContext(label="my_data", data={})
        assert ctx.template_key == "custom_my_data"

    def test_to_template_dict_returns_data(self) -> None:
        """Test to_template_dict returns the data dict."""
        ctx = CustomContext(
            label="test",
            data={"key1": "value1", "key2": 42},
        )
        d = ctx.to_template_dict()
        assert d == {"key1": "value1", "key2": 42}


class TestContextComposer:
    """Tests for ContextComposer fluent builder."""

    def test_empty_composer(self) -> None:
        """Test empty composer."""
        composer = ContextComposer()
        assert len(composer) == 0
        assert not composer
        assert composer.build() == {}

    def test_add_single_item(self) -> None:
        """Test adding a single context item."""
        composer = ContextComposer()
        composer.add(ParentSummary(task="Test"))

        assert len(composer) == 1
        assert composer.has("parent_summary")

        result = composer.build()
        assert "parent_summary" in result
        assert result["parent_summary"]["task"] == "Test"

    def test_add_multiple_items(self) -> None:
        """Test adding multiple context items."""
        composer = (
            ContextComposer()
            .add(ParentSummary(task="Parent task"))
            .add(SiblingResults(siblings=()))
            .add(SharedDecisions(decisions=()))
        )

        assert len(composer) == 3
        assert composer.has("parent_summary")
        assert composer.has("sibling_results")
        assert composer.has("shared_decisions")

    def test_add_if_true(self) -> None:
        """Test add_if with True condition."""
        composer = ContextComposer()
        composer.add_if(True, ParentSummary(task="Added"))

        assert len(composer) == 1
        assert composer.has("parent_summary")

    def test_add_if_false(self) -> None:
        """Test add_if with False condition."""
        composer = ContextComposer()
        composer.add_if(False, ParentSummary(task="Not added"))

        assert len(composer) == 0
        assert not composer.has("parent_summary")

    def test_add_optional_with_value(self) -> None:
        """Test add_optional with non-None value."""
        summary = ParentSummary(task="Optional")
        composer = ContextComposer().add_optional(summary)

        assert len(composer) == 1
        assert composer.has("parent_summary")

    def test_add_optional_with_none(self) -> None:
        """Test add_optional with None value."""
        composer = ContextComposer().add_optional(None)

        assert len(composer) == 0

    def test_add_all(self) -> None:
        """Test add_all with list of items."""
        items = [
            ParentSummary(task="P"),
            SiblingResults(siblings=()),
        ]
        composer = ContextComposer().add_all(items)

        assert len(composer) == 2

    def test_merge(self) -> None:
        """Test merging two composers."""
        composer1 = ContextComposer().add(ParentSummary(task="P"))
        composer2 = ContextComposer().add(SiblingResults(siblings=()))

        composer1.merge(composer2)

        assert len(composer1) == 2
        assert composer1.has("parent_summary")
        assert composer1.has("sibling_results")

    def test_has_any(self) -> None:
        """Test has_any with multiple keys."""
        composer = ContextComposer().add(ParentSummary(task="P"))

        assert composer.has_any("parent_summary", "sibling_results")
        assert not composer.has_any("sibling_results", "shared_decisions")

    def test_keys(self) -> None:
        """Test keys method."""
        composer = (
            ContextComposer()
            .add(ParentSummary(task="P"))
            .add(AncestorData(label="fixer", agent_id=uuid4(), role="m", task="T"))
        )

        keys = composer.keys()
        assert "parent_summary" in keys
        assert "ancestor_fixer" in keys

    def test_clear(self) -> None:
        """Test clear method."""
        composer = ContextComposer().add(ParentSummary(task="P"))
        assert len(composer) == 1

        composer.clear()
        assert len(composer) == 0

    def test_copy(self) -> None:
        """Test copy method."""
        composer1 = ContextComposer().add(ParentSummary(task="P"))
        composer2 = composer1.copy()

        # Modify original
        composer1.add(SiblingResults(siblings=()))

        # Copy should not be affected
        assert len(composer1) == 2
        assert len(composer2) == 1

    def test_method_chaining(self) -> None:
        """Test fluent method chaining."""
        result = (
            ContextComposer()
            .add(ParentSummary(task="Parent"))
            .add_if(True, SiblingResults(siblings=()))
            .add_optional(None)
            .add(CustomContext(label="extra", data={"key": "value"}))
            .build()
        )

        assert "parent_summary" in result
        assert "sibling_results" in result
        assert "custom_extra" in result
        assert len(result) == 3

    def test_later_items_override_same_key(self) -> None:
        """Test that later items with same key override earlier."""
        composer = (
            ContextComposer()
            .add(ParentSummary(task="First"))
            .add(ParentSummary(task="Second"))
        )

        result = composer.build()
        assert result["parent_summary"]["task"] == "Second"

    def test_repr(self) -> None:
        """Test string representation."""
        composer = (
            ContextComposer()
            .add(ParentSummary(task="P"))
            .add(SiblingResults(siblings=()))
        )

        repr_str = repr(composer)
        assert "ContextComposer" in repr_str
        assert "parent_summary" in repr_str
        assert "sibling_results" in repr_str

    def test_add_invalid_type_raises(self) -> None:
        """Test that adding non-ContextData raises TypeError."""
        composer = ContextComposer()
        with pytest.raises(TypeError, match="Expected ContextData"):
            composer.add("not a context data")  # type: ignore


class TestAncestorDataWithDynamicLabels:
    """Tests for AncestorData with various labels."""

    def test_multiple_ancestors_different_labels(self) -> None:
        """Test adding multiple ancestors with different labels."""
        composer = (
            ContextComposer()
            .add(AncestorData(label="boss", agent_id=uuid4(), role="boss", task="Goal"))
            .add(AncestorData(label="fixer", agent_id=uuid4(), role="manager", task="Fix"))
            .add(AncestorData(label="builder", agent_id=uuid4(), role="manager", task="Build"))
        )

        assert len(composer) == 3
        assert composer.has("ancestor_boss")
        assert composer.has("ancestor_fixer")
        assert composer.has("ancestor_builder")

        result = composer.build()
        assert result["ancestor_boss"]["role"] == "boss"
        assert result["ancestor_fixer"]["role"] == "manager"
        assert result["ancestor_builder"]["role"] == "manager"
