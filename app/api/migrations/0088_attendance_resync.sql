CREATE TABLE IF NOT EXISTS attendance_resync_job (
    id VARCHAR(64) PRIMARY KEY,
    schedule_key VARCHAR(32) UNIQUE,
    actor VARCHAR(64) NOT NULL,
    region VARCHAR(128),
    date_from VARCHAR(10) NOT NULL,
    date_to VARCHAR(10) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS attendance_resync_item (
    id SERIAL PRIMARY KEY,
    job_id VARCHAR(64) NOT NULL REFERENCES attendance_resync_job(id),
    emp_id VARCHAR(64) NOT NULL,
    date VARCHAR(10) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    before_value TEXT,
    after_value TEXT,
    error TEXT,
    UNIQUE(job_id, emp_id, date)
);
CREATE INDEX IF NOT EXISTS ix_attendance_resync_item_job_id ON attendance_resync_item(job_id);
