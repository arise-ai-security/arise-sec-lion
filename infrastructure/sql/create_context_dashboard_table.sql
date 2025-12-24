-- Context Dashboard Schema
-- Global key-value store for cross-session learning between workers

CREATE TABLE IF NOT EXISTS context_dashboard (
    entry_id UUID PRIMARY KEY,
    work_title VARCHAR(500) NOT NULL,
    objective TEXT NOT NULL,
    justification TEXT NOT NULL,
    work_analysis TEXT NOT NULL,
    worker_report JSONB NOT NULL,
    session_id UUID NOT NULL,
    worker_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    tags TEXT[] DEFAULT '{}'
);

-- Index for fast title lookups (relevance matching queries all titles)
CREATE INDEX IF NOT EXISTS idx_context_dashboard_title
ON context_dashboard (work_title);

-- Index for session-scoped queries (if needed in future)
CREATE INDEX IF NOT EXISTS idx_context_dashboard_session
ON context_dashboard (session_id);

-- Index for ordering by creation time
CREATE INDEX IF NOT EXISTS idx_context_dashboard_created
ON context_dashboard (created_at DESC);

-- Full-text search index on work_title for potential optimization
CREATE INDEX IF NOT EXISTS idx_context_dashboard_title_fts
ON context_dashboard USING gin (to_tsvector('english', work_title));
