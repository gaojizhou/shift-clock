-- ========================================================================
-- 加班放开：同一班次允许不同岗位
--
-- 原规则（0043 的 uq_attendance_active_emp_date_squad）：同一员工同一自然日，
--   每个班次最多一条有效(active)记录（出勤+加班共用），即「人不能同时在两岗」。
-- 新规则：去重维度由「人+日期+班次」改为「人+日期+班次+岗位」——同一班次内
--   可上多个不同岗位（各占一条 active、各占编制），仅完全相同的「班次+岗位」算重复。
--   不分班岗(squad=NULL)经 COALESCE 归一后同理：不同岗位也可并存。
--   出勤仍受 0041 的 uq_attendance_active_emp_date 约束（每人每日仅一条 active 出勤）。
-- ========================================================================

-- 旧的「班次唯一」索引让位给「班次+岗位唯一」
DROP INDEX IF EXISTS uq_attendance_active_emp_date_squad;

-- 班次+岗位唯一约束（出勤+加班共同参与；NULL 班次归一）
CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_active_emp_date_squad_post
    ON public.attendance_record (emp_id, natual_date, COALESCE(squad, '__noshift__'), post)
    WHERE status = 'active';
