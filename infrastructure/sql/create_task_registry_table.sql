-- Task Registry Projection Table (CQRS Read Model)
-- ================================================
-- Denormalized table for fast task deduplication lookups.
-- This is a CQRS projection - separate from event-sourced aggregates.
--
-- Performance characteristics:
-- - Check if registered: O(1) via PRIMARY KEY
-- - List all for root: O(n) via root_id index (n = tasks in this run)
-- - No event replay needed for reads

CREATE TABLE IF NOT EXISTS task_registry (
    -- Task key: SHA256 hash of normalized description (first 16 chars)
    -- Globally unique across all runs (hash collision negligible)
    task_key VARCHAR(16) NOT NULL,

    -- Root agent ID (execution run identifier)
    root_id UUID NOT NULL,

    -- Original task description (truncated for storage efficiency)
    task_description VARCHAR(200) NOT NULL,

    -- Agent that registered this task
    registered_by UUID NOT NULL,

    -- Parent agent ID (for hierarchy tracking)
    parent_id UUID,

    -- Timestamp for cleanup queries
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Primary key: task_key is globally unique
    PRIMARY KEY (task_key),

    -- Unique constraint: one task_key per root_id (belt + suspenders)
    -- This handles edge case where same task_key appears in different runs
    CONSTRAINT unique_root_task UNIQUE (root_id, task_key)
);

-- Index for listing all tasks in a run (for prompt context)
-- Query: SELECT * FROM task_registry WHERE root_id = $1
CREATE INDEX IF NOT EXISTS idx_task_registry_root_id
ON task_registry (root_id);

-- Index for cleanup queries (delete old runs by date)
-- Query: DELETE FROM task_registry WHERE created_at < $1
CREATE INDEX IF NOT EXISTS idx_task_registry_created_at
ON task_registry (created_at);

-- Note: Advisory locks are used for atomic registration (pg_advisory_xact_lock)
-- No additional database objects needed for locking.
