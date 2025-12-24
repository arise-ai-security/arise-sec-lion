"""FastAPI dependency injection configuration.

This module provides dependency functions for injecting services into routes.
"""

from typing import Annotated

from fastapi import Depends, Request

from core.application.execution_service import AgentExecutionService
from core.ports.context_dashboard_port import ContextDashboardPort
from core.ports.event_store_port import EventStorePort


def get_event_store(request: Request) -> EventStorePort:
    """Get the event store from application state.

    Args:
        request: FastAPI request object.

    Returns:
        EventStorePort instance.
    """
    return request.app.state.event_store


def get_context_dashboard(request: Request) -> ContextDashboardPort | None:
    """Get the context dashboard from application state.

    Args:
        request: FastAPI request object.

    Returns:
        ContextDashboardPort instance or None if not configured.
    """
    return getattr(request.app.state, "context_dashboard", None)


def get_execution_service(request: Request) -> AgentExecutionService:
    """Get the execution service from application state.

    Args:
        request: FastAPI request object.

    Returns:
        AgentExecutionService instance.
    """
    return request.app.state.execution_service


# Type aliases for cleaner route signatures
EventStoreDep = Annotated[EventStorePort, Depends(get_event_store)]
ContextDashboardDep = Annotated[ContextDashboardPort | None, Depends(get_context_dashboard)]
ExecutionServiceDep = Annotated[AgentExecutionService, Depends(get_execution_service)]
