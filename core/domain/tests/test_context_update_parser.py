"""Test cases for context_update_parser (XML parsing from worker output)."""

from core.domain.services.context_update_parser import (
    parse_context_update,
)
from core.domain.values.parsed_context import ParsedDecision, ParsedArtifact, ParsedUpdate


class TestParseContextUpdate:
    """Test cases for parse_context_update() function."""

    def test_parse_single_decision(self) -> None:
        """Test parsing a single decision."""
        result = """
        Some work output here...

        <context-update>
        <decision key="api_framework">
          <value>FastAPI</value>
          <rationale>Better async support</rationale>
        </decision>
        </context-update>
        """

        parsed = parse_context_update(result)

        assert parsed is not None
        assert len(parsed.decisions) == 1
        assert len(parsed.artifacts) == 0
        assert parsed.decisions[0].key == "api_framework"
        assert parsed.decisions[0].value == "FastAPI"
        assert parsed.decisions[0].rationale == "Better async support"

    def test_parse_single_output(self) -> None:
        """Test parsing a single output."""
        result = """
        Created the user model...

        <context-update>
        <output key="user_model">Created User model in models/user.py</output>
        </context-update>
        """

        parsed = parse_context_update(result)

        assert parsed is not None
        assert len(parsed.decisions) == 0
        assert len(parsed.artifacts) == 1
        assert parsed.artifacts[0].key == "user_model"
        assert parsed.artifacts[0].description == "Created User model in models/user.py"

    def test_parse_multiple_decisions_and_outputs(self) -> None:
        """Test parsing multiple decisions and outputs."""
        result = """
        <context-update>
        <decision key="orm_framework">
          <value>SQLAlchemy</value>
          <rationale>Type hints support</rationale>
        </decision>
        <decision key="testing_framework">
          <value>pytest</value>
          <rationale>Standard Python testing</rationale>
        </decision>
        <output key="database_schema">Created schema in db/schema.py</output>
        <output key="api_routes">Created routes in api/routes.py</output>
        </context-update>
        """

        parsed = parse_context_update(result)

        assert parsed is not None
        assert len(parsed.decisions) == 2
        assert len(parsed.artifacts) == 2
        assert parsed.decisions[0].key == "orm_framework"
        assert parsed.decisions[1].key == "testing_framework"
        assert parsed.artifacts[0].key == "database_schema"
        assert parsed.artifacts[1].key == "api_routes"

    def test_parse_no_context_update_returns_none(self) -> None:
        """Test that missing context-update section returns None."""
        result = "Just some regular output without any context update."

        parsed = parse_context_update(result)

        assert parsed is None

    def test_parse_empty_context_update_returns_none(self) -> None:
        """Test that empty context-update section returns None."""
        result = """
        <context-update>
        </context-update>
        """

        parsed = parse_context_update(result)

        assert parsed is None

    def test_parse_malformed_xml_returns_none(self) -> None:
        """Test that malformed XML returns None gracefully."""
        result = """
        <context-update>
        <decision key="test">
          <value>Missing closing tags
        </context-update>
        """

        parsed = parse_context_update(result)

        assert parsed is None

    def test_parse_decision_without_key_skipped(self) -> None:
        """Test that decision without key attribute is skipped."""
        result = """
        <context-update>
        <decision>
          <value>No key</value>
          <rationale>Should be skipped</rationale>
        </decision>
        <decision key="valid_key">
          <value>Valid</value>
          <rationale>This one counts</rationale>
        </decision>
        </context-update>
        """

        parsed = parse_context_update(result)

        assert parsed is not None
        assert len(parsed.decisions) == 1
        assert parsed.decisions[0].key == "valid_key"

    def test_parse_output_without_key_skipped(self) -> None:
        """Test that output without key attribute is skipped."""
        result = """
        <context-update>
        <output>No key here</output>
        <output key="valid_output">Valid output description</output>
        </context-update>
        """

        parsed = parse_context_update(result)

        assert parsed is not None
        assert len(parsed.artifacts) == 1
        assert parsed.artifacts[0].key == "valid_output"

    def test_parse_decision_without_rationale(self) -> None:
        """Test parsing decision without rationale (optional)."""
        result = """
        <context-update>
        <decision key="simple_choice">
          <value>Option A</value>
        </decision>
        </context-update>
        """

        parsed = parse_context_update(result)

        assert parsed is not None
        assert len(parsed.decisions) == 1
        assert parsed.decisions[0].key == "simple_choice"
        assert parsed.decisions[0].value == "Option A"
        assert parsed.decisions[0].rationale == ""

    def test_parse_case_insensitive_tags(self) -> None:
        """Test that context-update tag matching is case insensitive."""
        result = """
        <CONTEXT-UPDATE>
        <decision key="test">
          <value>Test value</value>
          <rationale>Test reason</rationale>
        </decision>
        </CONTEXT-UPDATE>
        """

        parsed = parse_context_update(result)

        assert parsed is not None
        assert len(parsed.decisions) == 1
        assert parsed.decisions[0].key == "test"

    def test_parse_whitespace_trimmed(self) -> None:
        """Test that whitespace is trimmed from values."""
        result = """
        <context-update>
        <decision key="formatted">
          <value>
            Some value with whitespace
          </value>
          <rationale>
            Some rationale with whitespace
          </rationale>
        </decision>
        <output key="formatted_output">
          Output with whitespace
        </output>
        </context-update>
        """

        parsed = parse_context_update(result)

        assert parsed is not None
        assert parsed.decisions[0].value == "Some value with whitespace"
        assert parsed.decisions[0].rationale == "Some rationale with whitespace"
        assert parsed.artifacts[0].description == "Output with whitespace"

    def test_context_update_at_end_of_output(self) -> None:
        """Test context-update at end of output (typical placement)."""
        result = """
        I've completed the implementation of the user authentication module.

        Key changes:
        - Created User model with email validation
        - Implemented password hashing using bcrypt
        - Added login/logout endpoints

        All tests passing.

        <context-update>
        <decision key="auth_method">
          <value>JWT</value>
          <rationale>Stateless authentication for API</rationale>
        </decision>
        <output key="auth_module">Implemented auth module in auth/</output>
        </context-update>
        """

        parsed = parse_context_update(result)

        assert parsed is not None
        assert len(parsed.decisions) == 1
        assert len(parsed.artifacts) == 1
        assert parsed.decisions[0].key == "auth_method"
        assert parsed.artifacts[0].key == "auth_module"


class TestParsedDecision:
    """Test cases for ParsedDecision dataclass."""

    def test_to_dict_serialization(self) -> None:
        """Test to_dict() serialization."""
        decision = ParsedDecision(
            key="framework",
            value="FastAPI",
            rationale="Better async",
        )

        data = decision.model_dump()

        assert data["key"] == "framework"
        assert data["value"] == "FastAPI"
        assert data["rationale"] == "Better async"

    def test_from_dict_deserialization(self) -> None:
        """Test from_dict() deserialization."""
        data = {
            "key": "framework",
            "value": "FastAPI",
            "rationale": "Better async",
        }

        decision = ParsedDecision.model_validate(data)

        assert decision.key == "framework"
        assert decision.value == "FastAPI"
        assert decision.rationale == "Better async"

    def test_from_dict_missing_rationale(self) -> None:
        """Test from_dict() with missing rationale defaults to empty string."""
        data = {
            "key": "framework",
            "value": "FastAPI",
        }

        decision = ParsedDecision.model_validate(data)

        assert decision.rationale == ""


class TestParsedArtifact:
    """Test cases for ParsedArtifact dataclass."""

    def test_to_dict_serialization(self) -> None:
        """Test to_dict() serialization."""
        output = ParsedArtifact(
            key="user_model",
            description="Created User model in models/",
        )

        data = output.model_dump()

        assert data["key"] == "user_model"
        assert data["description"] == "Created User model in models/"

    def test_from_dict_deserialization(self) -> None:
        """Test from_dict() deserialization."""
        data = {
            "key": "user_model",
            "description": "Created User model",
        }

        output = ParsedArtifact.model_validate(data)

        assert output.key == "user_model"
        assert output.description == "Created User model"


class TestParsedUpdate:
    """Test cases for ParsedUpdate dataclass."""

    def test_to_dict_serialization(self) -> None:
        """Test to_dict() serialization."""
        update = ParsedUpdate(
            decisions=(
                ParsedDecision(key="k1", value="v1", rationale="r1"),
            ),
            artifacts=(
                ParsedArtifact(key="o1", description="d1"),
            ),
        )

        data = update.model_dump()

        assert len(data["decisions"]) == 1
        assert len(data["artifacts"]) == 1
        assert data["decisions"][0]["key"] == "k1"
        assert data["artifacts"][0]["key"] == "o1"

    def test_from_dict_deserialization(self) -> None:
        """Test from_dict() deserialization."""
        data = {
            "decisions": [
                {"key": "k1", "value": "v1", "rationale": "r1"},
            ],
            "artifacts": [
                {"key": "o1", "description": "d1"},
            ],
        }

        update = ParsedUpdate.model_validate(data)

        assert len(update.decisions) == 1
        assert len(update.artifacts) == 1
        assert update.decisions[0].key == "k1"
        assert update.artifacts[0].key == "o1"

    def test_from_dict_empty_lists(self) -> None:
        """Test from_dict() with empty decisions/artifacts."""
        data = {}

        update = ParsedUpdate.model_validate(data)

        assert update.decisions == ()
        assert update.artifacts == ()
