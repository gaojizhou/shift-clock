-- ========================================================================
-- 加班功能：attendance_record 增列 + 班次唯一约束
--
-- 1) recon_dingtalk：是否核对钉钉考勤。存量出勤默认 true（口径不变）；加班业务（模式1
--    值班长改时长 / 模式2跨岗加班）写入时按需置 false（视为有效、不与钉钉对照）。
-- 2) 跨「出勤+加班」的班次唯一约束：同一员工同一自然日，每个班次最多一条有效(active)记录，
--    不同班次可并存（=跨岗位加班）。squad=NULL（不分班岗：值班长/维修工）经 COALESCE 归一为
--    哨兵值，故不分班岗同一天只能一条有效记录、无法再建第二条 → 不分班加班只能走「加时长」。
-- ========================================================================

ALTER TABLE public.attendance_record
    ADD COLUMN IF NOT EXISTS recon_dingtalk boolean NOT NULL DEFAULT true;
COMMENT ON COLUMN public.attendance_record.recon_dingtalk IS
    '是否核对钉钉考勤：true=纳入钉钉对账(默认)，false=视为有效不核对(加班默认)';

-- 防御性历史去重：同一 (emp_id, natual_date, 归一班次) 若已有多条 active，保留最早一条，
-- 其余置「已取消」，否则下面的唯一索引创建会失败（存量理论上无冲突，仅兜底）。
UPDATE public.attendance_record a SET status = '已取消'
FROM (
    SELECT id,
           row_number() OVER (
               PARTITION BY emp_id, natual_date, COALESCE(squad, '__noshift__')
               ORDER BY COALESCE(scan_time, '1970-01-01'::timestamptz), id
           ) AS rn
    FROM public.attendance_record
    WHERE status = 'active'
) d
WHERE a.id = d.id AND d.rn > 1;

-- 班次唯一约束（出勤+加班共同参与；NULL 班次归一）
CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_active_emp_date_squad
    ON public.attendance_record (emp_id, natual_date, COALESCE(squad, '__noshift__'))
    WHERE status = 'active';

-- 候选人/按类型查询索引
CREATE INDEX IF NOT EXISTS ix_attendance_emp_date_kind
    ON public.attendance_record (emp_id, natual_date, record_kind);
