-- 劳勤同步/转换的跨进程数据库租约。复用 dingtalk_sync_state 的专用状态行，
-- owner_token 由条件 UPDATE 原子抢占，避免多 uvicorn worker 或独立调度进程并发写。
ALTER TABLE public.dingtalk_sync_state
    ADD COLUMN IF NOT EXISTS owner_token character varying(36);

COMMENT ON COLUMN public.dingtalk_sync_state.owner_token IS
    'attendance_task_lease 专用 owner token；NULL 表示当前无劳勤任务持有租约';
