-- ========================================================================
-- 0057：laoqin_punch 增加 makeup_reason 列（补卡原因）
--
-- 背景：钉钉原始考勤 dingtalk_attendance_raw.src 区分来源——「钉钉打卡」=打卡机，
--   「工作流」=补卡审批（detail 为 {"reason":N,...}、address 空）。转换为劳勤打卡时
--   record_source 已落「工作流」标识，但补卡原因码(reason)未保留。
-- 本列存可读补卡原因（如「工作流补卡·漏卡」），非补卡为空；供劳勤工作台/导出区分补录。
-- 列宽 32 容纳中文（SQL_ASCII 库按字节计长，留足余量）。
-- ========================================================================

ALTER TABLE public.laoqin_punch ADD COLUMN IF NOT EXISTS makeup_reason character varying(32);
