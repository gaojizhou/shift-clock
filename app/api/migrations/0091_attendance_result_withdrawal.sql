ALTER TABLE external_attendance_result_raw ADD COLUMN IF NOT EXISTS source_withdrawn_at TIMESTAMPTZ;
