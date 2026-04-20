"""Unit tests for experiments.tree_projection pure helpers.

Integration with the event store requires Postgres and is exercised by
Task 16's smoke run.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from core.domain.events.events import ThoughtCaptured
from experiments.tree_projection import (
    _VALID_ROLES,
    _AgentState,
    _build_agent_index,
    _convert_event,
    _map_operation,
    _map_role,
    _parse_cell_from_run_dir,
    _parse_cve_from_run_dir,
    _parse_replicate_from_run_dir,
    _to_float_or_none,
    _to_int_or_none,
    _to_str_or_none,
)


if TYPE_CHECKING:
    from pathlib import Path


class TestMapOperation:
    def test_translates_legacy_complexity_evaluation_to_assess(self) -> None:
        # Given: the domain's legacy "complexity_evaluation" string
        # When: mapped
        # Then: returns the normalized "assess"
        assert _map_operation("complexity_evaluation") == "assess"

    def test_translates_task_decomposition_to_decompose(self) -> None:
        # Given: legacy "task_decomposition"
        # When / Then
        assert _map_operation("task_decomposition") == "decompose"

    def test_passes_through_worker_execution_verification_condense(self) -> None:
        # Given: operation strings already in normalized form
        # When / Then: pass-through
        assert _map_operation("worker_execution") == "worker_execution"
        assert _map_operation("verification") == "verification"
        assert _map_operation("context_condense") == "context_condense"

    def test_unknown_operation_passes_through_unchanged(self) -> None:
        # Unknown strings fall through; schema validation downstream will reject.
        assert _map_operation("something_new") == "something_new"


class TestMapRole:
    def test_uppercases_valid_role(self) -> None:
        # Given: lowercase "worker"
        # When: mapped
        # Then: returns "WORKER"
        assert _map_role("worker") == "WORKER"

    def test_unknown_role_defaults_to_pending(self) -> None:
        # Given: an unknown role string
        # When: mapped
        # Then: falls back to PENDING (schema-safe)
        assert _map_role("nonsense") == "PENDING"

    def test_empty_role_defaults_to_pending(self) -> None:
        assert _map_role("") == "PENDING"

    def test_all_valid_roles_round_trip(self) -> None:
        for role in ("boss", "manager", "worker", "pending", "judge", "condenser", "flat"):
            assert _map_role(role) == role.upper()

    def test_valid_roles_constant_matches_schema(self) -> None:
        # Defensive: the _VALID_ROLES set must cover every schema Role literal.
        assert frozenset(
            {"BOSS", "MANAGER", "WORKER", "PENDING", "JUDGE", "CONDENSER", "FLAT"}
        ) == _VALID_ROLES


class TestToolNameProjection:
    """Verify _convert_event reads tool_name directly from ThoughtCaptured."""

    def _make_state(self, agent_id: object = None) -> _AgentState:
        return _AgentState(
            agent_id=agent_id or uuid4(),
            role="WORKER",
            parent_id=None,
            depth=1,
        )

    def _make_thought(self, tool_name: str | None) -> ThoughtCaptured:
        return ThoughtCaptured(
            aggregate_id=uuid4(),
            sequence_number=1,
            content="Running: ls -la",
            stream="claude_sdk",
            output_type="tool_use",
            call_id="tu_1",
            tool_input_json={"command": "ls -la"},
            tool_name=tool_name,
        )

    def test_tool_name_from_event_used_directly(self) -> None:
        # Given: a ThoughtCaptured with tool_name="Bash" from the SDK
        ev = self._make_thought(tool_name="Bash")

        # When: projected
        result = _convert_event(ev, self._make_state(), "test-run", 1)

        # Then: payload uses the SDK tool_name, not regex
        assert result is not None
        assert result.payload.tool_name == "Bash"

    def test_none_tool_name_becomes_unknown(self) -> None:
        # Given: a historical ThoughtCaptured without tool_name
        ev = self._make_thought(tool_name=None)

        # When: projected
        result = _convert_event(ev, self._make_state(), "test-run", 1)

        # Then: falls back to "Unknown" (honest signal)
        assert result is not None
        assert result.payload.tool_name == "Unknown"

    def test_each_tool_type_preserved(self) -> None:
        # Given: events for each known tool type
        for name in ("Read", "Write", "Edit", "Bash", "Grep", "Glob"):
            ev = self._make_thought(tool_name=name)

            # When: projected
            result = _convert_event(ev, self._make_state(), "test-run", 1)

            # Then: tool_name is preserved exactly
            assert result is not None
            assert result.payload.tool_name == name


class TestRunDirParsing:
    def test_parse_cve_from_run_dir(self, tmp_path: Path) -> None:
        # Given: runner's directory layout dataset/runs/<cve>/<cell>/<replicate>
        run_dir = tmp_path / "runs" / "njs.cve-2022-32414" / "B1" / "0"
        run_dir.mkdir(parents=True)
        # When / Then
        assert _parse_cve_from_run_dir(run_dir) == "njs.cve-2022-32414"

    def test_parse_cell_from_valid(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "njs.cve-2022-32414" / "A1" / "0"
        run_dir.mkdir(parents=True)
        assert _parse_cell_from_run_dir(run_dir) == "A1"

    def test_parse_cell_from_unknown_defaults_b1(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "njs.cve-2022-32414" / "bogus" / "0"
        run_dir.mkdir(parents=True)
        # Cell label doesn't match the known set; fall back to B1 (schema-safe default)
        assert _parse_cell_from_run_dir(run_dir) == "B1"

    def test_parse_replicate_from_int_dir(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "njs.cve-2022-32414" / "B1" / "2"
        run_dir.mkdir(parents=True)
        assert _parse_replicate_from_run_dir(run_dir) == 2

    def test_parse_replicate_from_non_int_defaults_zero(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "njs.cve-2022-32414" / "B1" / "not_a_number"
        run_dir.mkdir(parents=True)
        assert _parse_replicate_from_run_dir(run_dir) == 0


class TestCoerceHelpers:
    def test_to_int_or_none_handles_none(self) -> None:
        assert _to_int_or_none(None) is None

    def test_to_int_or_none_coerces_int(self) -> None:
        assert _to_int_or_none("42") == 42
        assert _to_int_or_none(42) == 42
        assert _to_int_or_none(42.7) == 42

    def test_to_int_or_none_bad_input_returns_none(self) -> None:
        assert _to_int_or_none("not a number") is None

    def test_to_float_or_none(self) -> None:
        assert _to_float_or_none(None) is None
        assert _to_float_or_none("3.14") == pytest.approx(3.14)
        assert _to_float_or_none("bad") is None

    def test_to_str_or_none(self) -> None:
        assert _to_str_or_none(None) is None
        assert _to_str_or_none("") is None  # empty strings normalize to None
        assert _to_str_or_none("foo") == "foo"


class TestBuildAgentIndex:
    """Tests for depth computation and role tracking via the stateful index.

    Uses stub objects matching the DomainEvent surface (no Postgres).
    """

    def test_single_boss_depth_zero(self) -> None:
        # Given: a single AgentCreated event for the root BOSS
        boss_id = uuid4()

        class FakeCreated:
            def __init__(self) -> None:
                self.role = "boss"
                self.parent_id = None

        FakeCreated.__name__ = "AgentCreated"

        grouped = {boss_id: [FakeCreated()]}

        # When: index is built
        states = _build_agent_index(grouped, boss_id)  # type: ignore[arg-type]

        # Then: BOSS, depth 0, no parent
        assert states[boss_id].role == "BOSS"
        assert states[boss_id].parent_id is None
        assert states[boss_id].depth == 0

    def test_two_level_tree_depth_one(self) -> None:
        # Given: BOSS + one child MANAGER
        boss_id = uuid4()
        mgr_id = uuid4()

        class Created:
            def __init__(self, role: str, parent: object) -> None:
                self.role = role
                self.parent_id = parent

        Created.__name__ = "AgentCreated"

        grouped = {
            boss_id: [Created("boss", None)],
            mgr_id: [Created("manager", boss_id)],
        }

        states = _build_agent_index(grouped, boss_id)  # type: ignore[arg-type]

        # Then: manager has depth 1
        assert states[mgr_id].role == "MANAGER"
        assert states[mgr_id].parent_id == boss_id
        assert states[mgr_id].depth == 1

    def test_complexity_evaluated_promotes_pending_to_worker(self) -> None:
        # Given: a PENDING agent + a later ComplexityEvaluated determining WORKER
        boss_id = uuid4()
        agent_id = uuid4()

        class Created:
            def __init__(self) -> None:
                self.role = "pending"
                self.parent_id = boss_id

        Created.__name__ = "AgentCreated"

        class Evaluated:
            def __init__(self) -> None:
                self.determined_role = "worker"

        Evaluated.__name__ = "ComplexityEvaluated"

        grouped = {
            boss_id: [],
            agent_id: [Created(), Evaluated()],
        }

        states = _build_agent_index(grouped, boss_id)  # type: ignore[arg-type]
        # Then: role promoted from PENDING to WORKER
        assert states[agent_id].role == "WORKER"

    def test_cycle_detection_prevents_infinite_depth(self) -> None:
        # Given: two agents whose parent_id chain is cyclic (pathological)
        boss_id = uuid4()
        a_id = uuid4()
        b_id = uuid4()

        class Created:
            def __init__(self, role: str, parent: object) -> None:
                self.role = role
                self.parent_id = parent

        Created.__name__ = "AgentCreated"

        grouped = {
            boss_id: [Created("boss", None)],
            a_id: [Created("worker", b_id)],
            b_id: [Created("worker", a_id)],
        }
        # When: index built, should not hang
        states = _build_agent_index(grouped, boss_id)  # type: ignore[arg-type]
        # Then: depth computation is bounded (both agents get finite values)
        assert states[a_id].depth >= 0
        assert states[b_id].depth >= 0
        assert states[a_id].depth < 50
        assert states[b_id].depth < 50
