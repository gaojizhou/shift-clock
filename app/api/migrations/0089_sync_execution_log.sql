CREATE TABLE IF NOT EXISTS sync_execution_log (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(64) NOT NULL,
    trigger VARCHAR(16) NOT NULL,
    actor VARCHAR(64) NOT NULL,
    region VARCHAR(128),
    date_from VARCHAR(10),
    date_to VARCHAR(10),
    status VARCHAR(16) NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    counts_json TEXT NOT NULL DEFAULT '{}',
    message TEXT
);
CREATE INDEX IF NOT EXISTS ix_sync_execution_log_started_at ON sync_execution_log(started_at);
CREATE INDEX IF NOT EXISTS ix_sync_execution_log_kind ON sync_execution_log(kind);
CREATE INDEX IF NOT EXISTS ix_sync_execution_log_trigger ON sync_execution_log(trigger);
CREATE INDEX IF NOT EXISTS ix_sync_execution_log_region ON sync_execution_log(region);
CREATE INDEX IF NOT EXISTS ix_sync_execution_log_status ON sync_execution_log(status);
