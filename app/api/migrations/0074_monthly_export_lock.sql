-- 月度导出锁：锁定后禁止会改变该月导出结果的写操作。

CREATE TABLE IF NOT EXISTS public.monthly_export_lock (
    id serial PRIMARY KEY,
    month varchar(7) NOT NULL UNIQUE,
    locked boolean NOT NULL DEFAULT false,
    locked_by varchar(64),
    locked_at timestamp with time zone,
    unlocked_by varchar(64),
    unlocked_at timestamp with time zone,
    updated_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_monthly_export_lock_month_locked
    ON public.monthly_export_lock (month, locked);
