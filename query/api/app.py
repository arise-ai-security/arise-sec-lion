"""FastAPI application factory.

This module creates and configures the FastAPI application with all routes.
It properly initializes the event store using the lifespan context manager.
"""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import Settings
from core.ports.event_store_port import EventStorePort
from query.api.routes import agents, config, events, prompts


# Factory function injected by bootstrap layer (avoids query→infrastructure dependency)
_event_store_factory: Callable[[str], EventStorePort] | None = None


def set_event_store_factory(factory: Callable[[str], EventStorePort]) -> None:
    """Set the event store factory function.

    Called by bootstrap layer to inject the concrete implementation.
    This avoids query layer importing from infrastructure.
    """
    global _event_store_factory
    _event_store_factory = factory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifespan - startup and shutdown.

    This context manager:
    1. On startup: Connects to the database
    2. On shutdown: Disconnects from the database

    The event store is attached to app.state for access in routes.
    """
    if _event_store_factory is None:
        raise RuntimeError(
            "Event store factory not configured. "
            "Ensure bootstrap layer has initialized before starting the API. "
            "Use query.api.bootstrap:create_app instead of query.api.app:create_app"
        )

    # Load settings from config files and environment variables
    settings = Settings.load()

    # Create and connect the event store
    event_store = _event_store_factory(settings.database.connection_string)
    await event_store.connect()

    # Attach to app state for dependency injection in routes
    app.state.event_store = event_store

    yield

    # Cleanup on shutdown
    await event_store.disconnect()


def create_app(
    title: str = "Arise Sec Lion",
    static_dir: Path | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        title: Application title for OpenAPI docs.
        static_dir: Path to static files directory for React app.
                   If not provided, defaults to query/web/dist if it exists.

    Returns:
        Configured FastAPI application instance.
    """
    # Default to web dist directory if not specified
    if static_dir is None:
        default_static = Path(__file__).parent.parent / "web" / "dist"
        if default_static.exists():
            static_dir = default_static
    app = FastAPI(
        title=title,
        description="REST API and SSE endpoints for the multi-agent orchestration system",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # CORS middleware for development (React dev server on different port)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:3000"],  # Vite default ports
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include API routers
    app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
    app.include_router(events.router, prefix="/api/events", tags=["events"])
    app.include_router(prompts.router, prefix="/api/prompts", tags=["prompts"])
    app.include_router(config.router, prefix="/api/config", tags=["config"])

    # Health check endpoint
    @app.get("/api/health", tags=["health"])
    async def health_check() -> dict:
        """Health check endpoint."""
        return {"status": "healthy"}

    # Serve static files if directory provided (production build)
    if static_dir and static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
