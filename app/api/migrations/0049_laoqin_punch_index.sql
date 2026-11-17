-- ========================================================================
-- 为 laoqin_punch 增加索引：考勤数据量上来后，按 emp_id+natual_date / natual_date
-- 的逐人查询与对账会全表扫描，加索引消除扫描成本（recon/timeline 等接口提速）。
-- ========================================================================

CREATE INDEX IF NOT EXISTS ix_laoqin_punch_emp_date
    ON public.laoqin_punch (emp_id, natual_date);

CREATE INDEX IF NOT EXISTS ix_laoqin_punch_natual_date
    ON public.laoqin_punch (natual_date);
