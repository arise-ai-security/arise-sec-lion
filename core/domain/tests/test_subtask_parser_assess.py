"""Test cases for parse_assessment_response (TDD approach)."""

import json

import pytest

from core.domain.services.subtask_parser import parse_assessment_response


class TestParseAssessmentResponse:
    def test_execute_action(self):
        raw = '{"action": "execute", "reasoning": "Clear task"}'
        result = parse_assessment_response(raw)
        assert result.action == "execute"
        assert result.reasoning == "Clear task"
        assert result.subtasks is None

    def test_decompose_action(self):
        raw = json.dumps(
            {
                "action": "decompose",
                "reasoning": "Needs split",
                "subtasks": [
                    {
                        "description": "Sub 1",
                        "config": {
                            "strategy": "heuristic",
                            "base": {
                                "model": "gpt-4o-mini",
                                "temperature": 0.5,
                                "max_tokens": 4000,
                            },
                            "tool": "claude_code",
                        },
                    }
                ],
            }
        )
        result = parse_assessment_response(raw)
        assert result.action == "decompose"
        assert len(result.subtasks) == 1
        assert result.subtasks[0].description == "Sub 1"

    def test_constraint_failure(self):
        raw = '{"status": "constraints_unsatisfiable", "reason": "Too many"}'
        result = parse_assessment_response(raw)
        assert result.action == "infeasible"
        assert result.constraint_failure is not None
        assert result.constraint_failure.reason == "Too many"

    def test_invalid_action_raises(self):
        raw = '{"action": "unknown"}'
        with pytest.raises(ValueError, match="Invalid action"):
            parse_assessment_response(raw)

    def test_markdown_wrapped_json(self):
        raw = '```json\n{"action": "execute", "reasoning": "Simple"}\n```'
        result = parse_assessment_response(raw)
        assert result.action == "execute"

    def test_decompose_empty_subtasks_raises(self):
        raw = '{"action": "decompose", "reasoning": "Need split", "subtasks": []}'
        with pytest.raises(ValueError, match=r"[Ee]mpty"):
            parse_assessment_response(raw)

    def test_decompose_subtask_missing_config_uses_default(self):
        raw = json.dumps(
            {
                "action": "decompose",
                "reasoning": "Split",
                "subtasks": [{"description": "Sub 1"}],
            }
        )
        result = parse_assessment_response(raw)
        assert result.action == "decompose"
        assert result.subtasks is not None
        assert result.subtasks[0].description == "Sub 1"
        assert result.subtasks[0].config["tool"] == "claude_code"

    def test_execute_with_no_reasoning(self):
        raw = '{"action": "execute"}'
        result = parse_assessment_response(raw)
        assert result.action == "execute"
        assert result.reasoning == ""
