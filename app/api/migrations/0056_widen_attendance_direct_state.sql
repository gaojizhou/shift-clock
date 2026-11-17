-- ========================================================================
-- 0056：放宽 dingtalk_attendance_raw.direct / state 列宽到 varchar(32)
--
-- 背景：线上 ECS 的 fdc 库编码为 SQL_ASCII（按字节计长），考勤方向 direct='自动判断'
--   占 12 字节，超过原 varchar(8) → 导入/同步报 StringDataRightTruncation（前端只见
--   「请求失败」）。内网库为 UTF8（按字符计长，4 字符不溢出）一直正常。
-- 放宽到 32 后两种编码都能容纳；对 UTF8 库无副作用。
-- ========================================================================

ALTER TABLE public.dingtalk_attendance_raw ALTER COLUMN direct TYPE character varying(32);
ALTER TABLE public.dingtalk_attendance_raw ALTER COLUMN state TYPE character varying(32);
