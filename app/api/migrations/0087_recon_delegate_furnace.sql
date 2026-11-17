-- 固化 HR「待值班长确认」的目标工作点；历史委托为空时继续按考勤记录解析。
ALTER TABLE recon_result
    ADD COLUMN IF NOT EXISTS delegate_furnace_id INTEGER REFERENCES furnace(id);

CREATE INDEX IF NOT EXISTS ix_recon_result_delegate_furnace
    ON recon_result (delegate_furnace_id);
