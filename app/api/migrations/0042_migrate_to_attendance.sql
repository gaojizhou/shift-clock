-- ========================================================================
-- 部署时数据迁移：scan_record + makeup_request → attendance_record
--
-- 占比制 → 工时制：day_fraction 0.5→4.0(半班)，其余→8.0(整班)。
-- 旧表保留弃用（加 DEPRECATED 注释），仅供回滚与审计。
-- 幂等：由迁移执行器（schema_migration + SHA256）保证整个文件只执行一次。
-- ========================================================================

-- ① 历史扫码出勤：scan_record 全量迁入（含 active 与 已取消）
INSERT INTO public.attendance_record (
    emp_id, furnace_id, squad, squad_code, post, position_code, employ_type,
    scan_time, source, status, natual_date, work_hours, record_kind,
    settled_hours, legacy_table, legacy_id)
SELECT
    emp_id, furnace_id, squad, squad_code, post, position_code, employ_type,
    scan_time, source, status, natual_date,
    CASE WHEN day_fraction = 0.5 THEN 4.0 ELSE 8.0 END,
    '出勤',
    CASE WHEN day_fraction = 0.5 THEN 4.0 ELSE 8.0 END,
    'scan_record', id
FROM public.scan_record;

-- ② 在途补录/代签：仅迁 待审 与 拒绝。
--    已「通过」的补录/代签当初已写入 scan_record，已由 ① 迁入，避免重复计薪。
INSERT INTO public.attendance_record (
    emp_id, furnace_id, squad, squad_code, post, position_code, employ_type,
    source, status, natual_date, work_hours, record_kind, reason,
    reviewed_by, legacy_table, legacy_id)
SELECT
    emp_id, furnace_id, squad, squad_code, post, position_code, employ_type,
    kind,                                                   -- 补录 / 代签
    status,                                                 -- 待审 / 拒绝
    natual_date,
    CASE WHEN day_fraction = 0.5 THEN 4.0 ELSE 8.0 END,
    '出勤', reason, approver,
    'makeup_request', id
FROM public.makeup_request
WHERE status IN ('待审', '拒绝');

-- ③ 主键序列对齐，避免后续插入与已迁入 id 冲突（Postgres）
SELECT setval('public.attendance_record_id_seq',
              COALESCE((SELECT MAX(id) FROM public.attendance_record), 1), true);

-- ④ 冻结旧表（保留数据，仅供回滚/审计）
COMMENT ON TABLE public.scan_record    IS 'DEPRECATED 2026-06-19: 已迁至 attendance_record（工时制），仅留存历史与回滚。';
COMMENT ON TABLE public.makeup_request IS 'DEPRECATED 2026-06-19: 已并入 attendance_record（工时制），仅留存历史与回滚。';
