"""FastAPI Web API for the multi-agent system.

This module provides HTTP REST API and SSE endpoints for the frontend visualization.
"""

from presentation.api.app import create_app


__all__ = ["create_app"]
