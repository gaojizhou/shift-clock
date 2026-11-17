-- HR/统计首次确认页面展示地区。存量账号保持 NULL，升级后需重新选择一次。

ALTER TABLE public.employee
    ADD COLUMN IF NOT EXISTS region_confirmed_at timestamptz;

-- 已有地区的 HR/统计直接沿用；没有地区的账号保持 NULL，首次进入时才选择。
UPDATE public.employee e
SET region_confirmed_at = now()
WHERE e.role IN ('hr', 'stat')
  AND e.region_confirmed_at IS NULL
  AND EXISTS (
      SELECT 1
      FROM public.employee_favorite f
      WHERE f.emp_id = e.emp_id AND f.kind = 'region'
  );

COMMENT ON COLUMN public.employee.region_confirmed_at
    IS 'HR/统计已确认页面展示地区的时间；地区值复用 employee_favorite(kind=region)';
