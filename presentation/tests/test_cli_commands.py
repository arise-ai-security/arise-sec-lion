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

    def test_save_last_run_writes_atomically(self, tmp_path: Path) -> None:
        """A successful save_last_run must leave NO leftover .tmp file in the
        target directory. The atomic-write helper writes to mkstemp then
        renames so an interrupted writer can't leave a half-written pointer
        visible to interactive consumers (``main.py last``).
        """
        persistence = RunPersistence(tmp_path)
        persistence.save_last_run(uuid4(), "task", "completed")

        leftovers = list(tmp_path.glob(".last_run.json.*"))
        assert leftovers == []

    def test_save_last_run_preserves_previous_file_on_writer_failure(
        self, tmp_path: Path
    ) -> None:
        """If the writer raises mid-stream, the previous .last_run.json must
        survive intact. Pre-fix `write_text` could leave a truncated file
        visible to readers.
        """
        from unittest.mock import patch

        persistence = RunPersistence(tmp_path)
        first_id = uuid4()
        persistence.save_last_run(first_id, "first", "completed")
        previous_bytes = (tmp_path / ".last_run.json").read_bytes()

        # Force the second write to raise after creating the tmp file by
        # patching json.dump (called by _atomic_write_json) to blow up.
        def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated mid-write kill")

        with patch(
            "presentation.persistence.run_persistence.json.dump", _boom
        ):
            try:
                persistence.save_last_run(uuid4(), "second", "completed")
            except RuntimeError:
                pass

        # Then: the original file is byte-identical, no .tmp leftover.
        assert (tmp_path / ".last_run.json").read_bytes() == previous_bytes
        assert list(tmp_path.glob(".last_run.json.*")) == []


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
