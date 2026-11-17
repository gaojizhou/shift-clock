-- 员工差异自动同步配置、白名单和后台运行状态。自动开关默认关闭。

CREATE TABLE IF NOT EXISTS public.dingtalk_employee_sync_config (
    id integer PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT false,
    rules_json text NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by varchar(64)
);

INSERT INTO public.dingtalk_employee_sync_config (id, enabled, rules_json)
VALUES (1, false, '{}')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.dingtalk_employee_sync_whitelist (
    emp_id varchar(32) PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by varchar(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS public.dingtalk_employee_sync_run (
    id bigserial PRIMARY KEY,
    trigger varchar(16) NOT NULL,
    actor varchar(64) NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'running',
    phase varchar(32),
    rule_json text NOT NULL DEFAULT '{}',
    result_json text,
    message text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS ix_dingtalk_employee_sync_run_started_at
    ON public.dingtalk_employee_sync_run (started_at DESC);
