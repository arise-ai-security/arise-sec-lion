"""Bootstrap entry point for the Query API.

This module sets up infrastructure dependencies before creating the FastAPI app.
Use this module instead of query.api.app for proper dependency injection.

Usage:
    uvicorn query.api.bootstrap:create_app --factory --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI

from bootstrap.composition import build_domain_plugin
from infrastructure.adapters.postgres_event_store import PostgresEventStore
from query.api.app import (
    create_app as _create_app,
    set_domain_plugin_factory,
    set_event_store_factory,
)


def create_app() -> FastAPI:
    """Create the FastAPI app with all dependencies injected.

    This is the proper entry point for uvicorn. It:
    1. Injects the event store factory
    2. Injects the domain plugin factory
    3. Returns the configured FastAPI app
    """
    # Inject factories (bootstrap → query, not query → infrastructure/plugins)
    set_event_store_factory(PostgresEventStore)
    set_domain_plugin_factory(build_domain_plugin)

    return _create_app()
