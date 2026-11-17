-- ========================================================================
-- notification_record 增列，支持「班次结束推送」记录（已推/未推都落库，超管管理页可查）。
--   status：sent=已推送 / skipped=未推送（如值班长无钉钉号）
--   kind  ：通知类型，班次推送为 shift_push（其余历史记录留空）
-- 其余明细（furnace/squad/date/chief/summary/reason）存现有 payload(JSON)。
-- ========================================================================

ALTER TABLE public.notification_record ADD COLUMN status character varying(16) NOT NULL DEFAULT '';
ALTER TABLE public.notification_record ADD COLUMN kind character varying(32) NOT NULL DEFAULT '';

COMMENT ON COLUMN public.notification_record.status IS '推送状态：sent=已推送 / skipped=未推送（如无钉钉号）';
COMMENT ON COLUMN public.notification_record.kind IS '通知类型：shift_push=班次结束推送（历史记录留空）';
