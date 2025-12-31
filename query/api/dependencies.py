"""FastAPI dependency injection configuration.

This module provides dependency functions for injecting services into routes.
Uses segregated interfaces (ISP) - read-only endpoints depend on EventStoreReadPort.
"""

from typing import Annotated

from fastapi import Depends, Request

from core.application.execution_service import AgentExecutionService
from core.ports.event_store_port import EventStoreReadPort


def get_event_store(request: Request) -> EventStoreReadPort:
    """Get the event store from application state.

    Returns the read-only interface since query routes only need read access.

    Args:
        request: FastAPI request object.

    Returns:
        EventStoreReadPort instance.
    """
    return request.app.state.event_store


def get_execution_service(request: Request) -> AgentExecutionService:
    """Get the execution service from application state.

    Args:
        request: FastAPI request object.

    Returns:
        AgentExecutionService instance.
    """
    return request.app.state.execution_service


# Type aliases for cleaner route signatures
# Query API uses read-only port (ISP - Interface Segregation Principle)
type EventStoreDep = Annotated[EventStoreReadPort, Depends(get_event_store)]
type ExecutionServiceDep = Annotated[AgentExecutionService, Depends(get_execution_service)]
