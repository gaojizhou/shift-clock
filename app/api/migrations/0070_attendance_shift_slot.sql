-- ========================================================================
-- 加班时段拆分：AttendanceRecord 新增 shift_slot
--
-- squad 继续表示编制班组；shift_slot 仅加班记录使用，表示早/中/晚时段。
-- 历史加班数据按产品决策直接清空，避免旧 squad 兼任时段导致含义混乱。
-- ========================================================================

ALTER TABLE public.attendance_record
    ADD COLUMN IF NOT EXISTS shift_slot varchar(16);

DELETE FROM public.attendance_record
WHERE record_kind = '加班';

DROP INDEX IF EXISTS uq_attendance_active_emp_date_squad_post;

CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_active_emp_date_slot_post
    ON public.attendance_record (emp_id, natual_date, COALESCE(shift_slot, '__base__'), post)
    WHERE status = 'active';

