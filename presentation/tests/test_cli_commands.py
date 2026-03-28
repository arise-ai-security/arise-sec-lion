"""Tests for CLI commands."""



import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from bootstrap.bootstrap import _create_parser as create_parser, main
from presentation.persistence import RunPersistence


class TestRunPersistence:
    def test_save_last_run_creates_file(self, tmp_path: Path) -> None:
        persistence = RunPersistence(tmp_path)
        boss_id = uuid4()
        persistence.save_last_run(boss_id, "test task", "completed")
        assert json.loads((tmp_path / ".last_run.json").read_text())["boss_id"] == str(boss_id)

    def test_get_last_run_returns_data(self, tmp_path: Path) -> None:
        persistence = RunPersistence(tmp_path)
        boss_id = uuid4()
        persistence.save_last_run(boss_id, "test task", "completed")
        assert persistence.get_last_run().boss_id == boss_id

    def test_get_last_run_id_returns_none_when_not_found(self, tmp_path: Path) -> None:
        assert RunPersistence(tmp_path).get_last_run_id() is None


class TestCLIArgumentParser:
    def test_parser_run_command(self) -> None:
        args = create_parser().parse_args(["run", "test task"])
        assert args.command == "run" and args.task == "test task"

    def test_parser_events_command(self) -> None:
        test_uuid = str(uuid4())
        args = create_parser().parse_args(["events", "--agent-id", test_uuid, "--errors-only"])
        assert str(args.agent_id) == test_uuid and args.errors_only

    def test_parser_summary_command(self) -> None:
        args = create_parser().parse_args(["summary", "--format", "text"])
        assert args.command == "summary" and args.format == "text"

    def test_parser_list_command(self) -> None:
        args = create_parser().parse_args(["list", "--limit", "5"])
        assert args.limit == 5

    def test_parser_config_option(self) -> None:
        args = create_parser().parse_args(["--config", "/path/to/config.yaml", "list"])
        assert args.config == Path("/path/to/config.yaml")


def _mock_settings(tmp_path: Path = None):
    return MagicMock(
        output=MagicMock(directory=str(tmp_path or "/tmp")),
        database=MagicMock(connection_string="postgresql://test:test@localhost/test"),
    )


def _mock_event_store_cm(**store_methods):
    """Create mock async context manager for _event_store."""
    store = MagicMock()
    for name, value in store_methods.items():
        setattr(store, name, AsyncMock(return_value=value))

    @asynccontextmanager
    async def mock_cm(settings):
        yield store

    return mock_cm


class TestEventsCommand:
    def test_events_requires_agent_id_or_last_run(self, tmp_path: Path, capsys) -> None:
        with patch("config.Settings.load", return_value=_mock_settings(tmp_path)):
            with patch("bootstrap.bootstrap._event_store", _mock_event_store_cm()):
                main(["events"])
        assert "No agent-id specified" in capsys.readouterr().out


class TestListCommand:
    def test_list_with_no_runs_shows_message(self, capsys) -> None:
        with patch("config.Settings.load", return_value=_mock_settings()):
            with patch("bootstrap.bootstrap._event_store", _mock_event_store_cm(get_all_events_grouped={})):
                main(["list"])
        assert "No BOSS runs found" in capsys.readouterr().out

    def test_list_format_json_outputs_json(self, capsys) -> None:
        with patch("config.Settings.load", return_value=_mock_settings()):
            with patch("bootstrap.bootstrap._event_store", _mock_event_store_cm(get_all_events_grouped={})):
                main(["list", "--format", "json"])
        assert json.loads(capsys.readouterr().out) == []
