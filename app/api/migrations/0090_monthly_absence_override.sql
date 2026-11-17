CREATE TABLE IF NOT EXISTS monthly_absence_override (
    month VARCHAR(7) NOT NULL,
    emp_id VARCHAR(64) NOT NULL,
    days NUMERIC(4,2) CHECK (days >= 0 AND days <= 31),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_by VARCHAR(64) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (month, emp_id)
);
