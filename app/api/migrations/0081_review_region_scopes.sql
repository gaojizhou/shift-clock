-- Review 修复：模板初始化标记补地区 code；月度导出锁按地区隔离。

ALTER TABLE public.quota_template_scope
    ADD COLUMN IF NOT EXISTS region_code character varying(16);

UPDATE public.quota_template_scope scope
SET region_code = region.code
FROM public.region region
WHERE scope.region_code IS NULL
  AND region.name = scope.region
  AND region.deleted = false;

ALTER TABLE public.quota_template_scope
    ADD CONSTRAINT quota_template_scope_region_code_fkey
    FOREIGN KEY (region_code) REFERENCES public.region(code);

ALTER TABLE public.monthly_export_lock
    ADD COLUMN IF NOT EXISTS region character varying(64)
    NOT NULL DEFAULT '__global__';

ALTER TABLE public.monthly_export_lock
    DROP CONSTRAINT IF EXISTS monthly_export_lock_month_key;

ALTER TABLE public.monthly_export_lock
    ADD CONSTRAINT monthly_export_lock_month_region_key UNIQUE (month, region);

DROP INDEX IF EXISTS public.idx_monthly_export_lock_month_locked;
CREATE INDEX idx_monthly_export_lock_month_region_locked
    ON public.monthly_export_lock (month, region, locked);

COMMENT ON COLUMN public.monthly_export_lock.region IS
    '月锁地区；__global__ 表示管理员全局锁，地区名表示仅约束该地区';
