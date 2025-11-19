-- Event Store Schema
-- This table stores domain events in an append-only log with Optimistic Concurrency Control

CREATE TABLE IF NOT EXISTS events (
    event_id UUID PRIMARY KEY,
    aggregate_id UUID NOT NULL,
    sequence_number INT NOT NULL,
    event_type VARCHAR(255) NOT NULL,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    metadata JSONB DEFAULT '{}',
    CONSTRAINT unique_aggregate_sequence UNIQUE (aggregate_id, sequence_number)
);

-- Index for fast aggregate queries
CREATE INDEX IF NOT EXISTS idx_events_aggregate_id
ON events (aggregate_id);

-- Composite index for ordered event retrieval
CREATE INDEX IF NOT EXISTS idx_events_aggregate_sequence
ON events (aggregate_id, sequence_number);
