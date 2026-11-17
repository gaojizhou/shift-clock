-- 清理旧班次推送与扫码就近配置。推送配置将按全工作点当日小结重新生成默认值。

DELETE FROM public.app_setting
WHERE key IN ('shift_push_config', 'shift_proximity_hours');
