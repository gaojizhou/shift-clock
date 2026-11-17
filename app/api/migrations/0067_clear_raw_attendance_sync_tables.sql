-- 0067：清空「原始考勤同步」页维护的两张源数据表。
-- 后续重新按本地现存员工工号拉取，避免历史全量源数据继续拖慢查询。

TRUNCATE TABLE public.dingtalk_attendance_raw RESTART IDENTITY;
TRUNCATE TABLE public.external_attendance_result_raw RESTART IDENTITY;
