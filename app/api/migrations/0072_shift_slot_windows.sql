-- ========================================================================
-- 时段窗口修正：squad_schedule 只表示早/中/晚时段
-- ========================================================================

UPDATE public.squad_schedule
SET squad_code = NULL;

UPDATE public.squad_schedule
SET deleted = true
WHERE squad NOT IN ('早班', '中班', '晚班');

INSERT INTO public.squad_schedule (
    region, squad, squad_code, start_time, end_time,
    created_at, updated_at, created_by, updated_by, deleted
)
VALUES
    ('示例厂区A', '早班', NULL, '10:00', '18:00', now(), now(), 'migration', 'migration', false),
    ('示例厂区A', '中班', NULL, '18:00', '02:00', now(), now(), 'migration', 'migration', false),
    ('示例厂区A', '晚班', NULL, '02:00', '10:00', now(), now(), 'migration', 'migration', false),
    ('示例厂区B', '早班', NULL, '09:00', '17:00', now(), now(), 'migration', 'migration', false),
    ('示例厂区B', '中班', NULL, '17:00', '01:00', now(), now(), 'migration', 'migration', false),
    ('示例厂区B', '晚班', NULL, '01:00', '09:00', now(), now(), 'migration', 'migration', false)
ON CONFLICT (region, squad) DO UPDATE
SET squad_code = NULL,
    start_time = EXCLUDED.start_time,
    end_time = EXCLUDED.end_time,
    updated_at = now(),
    updated_by = 'migration',
    deleted = false;

COMMENT ON TABLE public.squad_schedule IS '时段时间表：每地区早/中/晚时段的起止时间，供加班打卡覆盖核对使用。';
COMMENT ON COLUMN public.squad_schedule.squad IS '时段名：早班/中班/晚班，不是编制班组。';
COMMENT ON COLUMN public.attendance_record.shift_slot IS '加班时段：早班/中班/晚班；基础出勤为空。';
