UPDATE public.attendance_record
SET recon_dingtalk = true
WHERE recon_dingtalk IS DISTINCT FROM true;

COMMENT ON COLUMN public.attendance_record.recon_dingtalk IS
    '历史字段：统一按 true 处理，值班长端不再允许控制是否核对钉钉考勤。';
