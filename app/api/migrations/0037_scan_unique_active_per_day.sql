-- ========================================================================
-- 唯一约束：同一员工同一自然日只能有一条有效(active)扫码记录。
--
-- 修复两类缺陷（自动化测试第1/2轮）：
--   1. 并发重复扫码竞态：/scan 的"先查后插"应用层防重在并发下失效，同人同日落多条。
--   2. 代签/补录跨工作点双出勤(P0)：makeup_approve 仅按 emp+date 定位、跨工作点再插 active 扫码，
--      1 次额度可让 1 人同日在多工作点各 1 条有效出勤 → 算薪成倍虚增。
-- DB 级唯一约束兜底应用层逻辑，并发与跨工作点路径均由数据库拒绝。
--
-- 先去重历史脏数据：每个 (emp_id, natual_date) 保留最早一条有效记录，其余置「已取消」，
-- 否则唯一索引创建会失败。
-- ========================================================================

UPDATE public.scan_record s SET status = '已取消'
FROM (
    SELECT id,
           row_number() OVER (PARTITION BY emp_id, natual_date ORDER BY scan_time, id) AS rn
    FROM public.scan_record
    WHERE status <> '已取消'
) d
WHERE s.id = d.id AND d.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_scan_active_emp_date
    ON public.scan_record (emp_id, natual_date)
    WHERE status <> '已取消';
