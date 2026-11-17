-- 补齐 view_external_att_result 假期字段，按原始字符串保存，仅用于同步与展示。
-- 存量记录保留 NULL，部署后按日期范围重新拉取考勤结果即可补齐。
ALTER TABLE public.external_attendance_result_raw
    ADD COLUMN leave_04 varchar(255),
    ADD COLUMN leave_05 varchar(255),
    ADD COLUMN leave_06 varchar(255),
    ADD COLUMN leave_07 varchar(255),
    ADD COLUMN leave_09 varchar(255),
    ADD COLUMN leave_10 varchar(255),
    ADD COLUMN leave_12 varchar(255),
    ADD COLUMN leave_13 varchar(255);

COMMENT ON COLUMN public.external_attendance_result_raw.leave_01 IS '年休假（小时）';
COMMENT ON COLUMN public.external_attendance_result_raw.leave_02 IS '排休假（小时）';
COMMENT ON COLUMN public.external_attendance_result_raw.leave_03 IS '事假（小时）';
COMMENT ON COLUMN public.external_attendance_result_raw.leave_04 IS '全薪病假（小时）';
COMMENT ON COLUMN public.external_attendance_result_raw.leave_05 IS '病假（小时）';
COMMENT ON COLUMN public.external_attendance_result_raw.leave_06 IS '计生假（源值，单位待确认）';
COMMENT ON COLUMN public.external_attendance_result_raw.leave_07 IS '婚假（小时）';
COMMENT ON COLUMN public.external_attendance_result_raw.leave_09 IS '产假（小时）';
COMMENT ON COLUMN public.external_attendance_result_raw.leave_10 IS '陪产假（小时）';
COMMENT ON COLUMN public.external_attendance_result_raw.leave_12 IS '丧假（小时）';
COMMENT ON COLUMN public.external_attendance_result_raw.leave_13 IS '工伤假（小时）';
