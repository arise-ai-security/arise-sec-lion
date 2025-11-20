"""Application layer for orchestrating domain logic with infrastructure.

This layer sits between the domain and infrastructure, coordinating
the execution flow and managing cross-cutting concerns like:
- Loading/saving aggregates from event store
- Optimistic Concurrency Control (OCC)
- Retry logic
- Error handling
"""
