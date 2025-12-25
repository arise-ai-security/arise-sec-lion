-- Context Dashboard Schema
-- Global key-value store for cross-session learning between workers

CREATE TABLE IF NOT EXISTS context_dashboard (
    entry_id UUID PRIMARY KEY,
    entry_type VARCHAR(20) NOT NULL DEFAULT 'worker',
    work_title VARCHAR(500) NOT NULL,
    objective TEXT NOT NULL,
    justification TEXT NOT NULL,
    work_analysis TEXT NOT NULL DEFAULT '',
    worker_report JSONB,
    source_context JSONB,
    session_id UUID NOT NULL,
    worker_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    tags TEXT[] DEFAULT '{}'
);

-- Add new columns if they don't exist (for migration)
DO $$
BEGIN
    -- Add entry_type column if it doesn't exist
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'context_dashboard' AND column_name = 'entry_type'
    ) THEN
        ALTER TABLE context_dashboard ADD COLUMN entry_type VARCHAR(20) NOT NULL DEFAULT 'worker';
    END IF;

    -- Add source_context column if it doesn't exist
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'context_dashboard' AND column_name = 'source_context'
    ) THEN
        ALTER TABLE context_dashboard ADD COLUMN source_context JSONB;
    END IF;

    -- Make worker_report nullable for SOURCE type entries (only if currently NOT NULL)
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'context_dashboard'
          AND column_name = 'worker_report'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE context_dashboard ALTER COLUMN worker_report DROP NOT NULL;
    END IF;

    -- Make work_analysis have a default (safe to run multiple times)
    ALTER TABLE context_dashboard ALTER COLUMN work_analysis SET DEFAULT '';
EXCEPTION
    WHEN others THEN
        -- Ignore errors during migration (e.g., column already has the right properties)
        NULL;
END $$;

-- Index for fast title lookups (relevance matching queries all titles)
CREATE INDEX IF NOT EXISTS idx_context_dashboard_title
ON context_dashboard (work_title);

-- Index for session-scoped queries (if needed in future)
CREATE INDEX IF NOT EXISTS idx_context_dashboard_session
ON context_dashboard (session_id);

-- Index for ordering by creation time
CREATE INDEX IF NOT EXISTS idx_context_dashboard_created
ON context_dashboard (created_at DESC);

-- Index for entry type filtering (e.g., find all SOURCE entries)
CREATE INDEX IF NOT EXISTS idx_context_dashboard_entry_type
ON context_dashboard (entry_type);

-- Full-text search index on work_title for potential optimization
CREATE INDEX IF NOT EXISTS idx_context_dashboard_title_fts
ON context_dashboard USING gin (to_tsvector('english', work_title));
