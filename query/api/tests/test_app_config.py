"""API config-loading tests."""

from __future__ import annotations

import os

from query.api.bootstrap import create_app


def test_api_boots_without_experiment_models(monkeypatch) -> None:
    """Query API uses ApiSettings, so base config may omit run model names."""

    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")

    app = create_app()

    assert app.state.settings.worker.tool == "openhands"
    assert app.state.settings.database.password == os.environ["POSTGRES_PASSWORD"]
