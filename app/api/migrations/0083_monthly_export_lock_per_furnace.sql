-- 月度导出锁改为「月份 + 工作点」粒度。
-- 兼容 0081 的「全局/地区」锁：全局锁展开到全部工作点，地区锁只展开到本地区工作点；
-- 同月同工作点同时命中多条历史范围时，locked=true 优先，避免已封账数据被意外解锁。

ALTER TABLE public.monthly_export_lock
    ADD COLUMN furnace_id integer REFERENCES public.furnace(id);

ALTER TABLE public.monthly_export_lock
    DROP CONSTRAINT IF EXISTS monthly_export_lock_month_key;

ALTER TABLE public.monthly_export_lock
    DROP CONSTRAINT IF EXISTS monthly_export_lock_month_region_key;

INSERT INTO public.monthly_export_lock (
    month,
    furnace_id,
    region,
    locked,
    locked_by,
    locked_at,
    unlocked_by,
    unlocked_at,
    updated_at
)
SELECT DISTINCT ON (legacy.month, furnace.id)
    legacy.month,
    furnace.id,
    legacy.region,
    legacy.locked,
    legacy.locked_by,
    legacy.locked_at,
    legacy.unlocked_by,
    legacy.unlocked_at,
    legacy.updated_at
FROM public.monthly_export_lock AS legacy
JOIN public.furnace AS furnace
  ON legacy.region = '__global__' OR legacy.region = furnace.region
WHERE legacy.furnace_id IS NULL
ORDER BY legacy.month, furnace.id, legacy.locked DESC, legacy.updated_at DESC;

DELETE FROM public.monthly_export_lock
WHERE furnace_id IS NULL;

ALTER TABLE public.monthly_export_lock
    ALTER COLUMN furnace_id SET NOT NULL;

DROP INDEX IF EXISTS public.idx_monthly_export_lock_month_locked;
DROP INDEX IF EXISTS public.idx_monthly_export_lock_month_region_locked;

ALTER TABLE public.monthly_export_lock
    DROP COLUMN region;

ALTER TABLE public.monthly_export_lock
    ADD CONSTRAINT uq_monthly_export_lock_month_furnace UNIQUE (month, furnace_id);

CREATE INDEX idx_monthly_export_lock_month_furnace_locked
    ON public.monthly_export_lock (month, furnace_id, locked);
