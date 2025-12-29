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

-- Index for BOSS agent queries (filters on AgentCreated events with role='boss')
CREATE INDEX IF NOT EXISTS idx_events_boss_agents
ON events (occurred_at DESC, aggregate_id)
WHERE event_type = 'AgentCreated' AND payload->>'role' = 'boss';

-- Index for children queries (filters on AgentCreated events by parent_id)
CREATE INDEX IF NOT EXISTS idx_events_parent_id
ON events ((payload->>'parent_id'))
WHERE event_type = 'AgentCreated' AND payload->>'parent_id' IS NOT NULL;
