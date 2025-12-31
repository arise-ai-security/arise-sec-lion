"""FastAPI dependency injection configuration.

This module provides dependency functions for injecting services into routes.
Uses segregated interfaces (ISP) - read-only endpoints depend on EventStoreReadPort.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from core.application.execution_service import AgentExecutionService
from core.ports.event_store_port import EventStoreReadPort
from core.ports.task_registry_port import TaskRegistryPort


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


def get_task_registry(request: Request) -> TaskRegistryPort:
    """Get the task registry from application state.

    Args:
        request: FastAPI request object.

    Returns:
        TaskRegistryPort instance.

    Raises:
        HTTPException: If task registry is not configured.
    """
    task_registry = request.app.state.task_registry
    if task_registry is None:
        raise HTTPException(
            status_code=503,
            detail="Task registry not configured",
        )
    return task_registry


# Type aliases for cleaner route signatures
# Query API uses read-only port (ISP - Interface Segregation Principle)
type EventStoreDep = Annotated[EventStoreReadPort, Depends(get_event_store)]
type ExecutionServiceDep = Annotated[AgentExecutionService, Depends(get_execution_service)]
type TaskRegistryDep = Annotated[TaskRegistryPort, Depends(get_task_registry)]
