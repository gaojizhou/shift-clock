-- 0078 把模板唯一空间改为“地区 + 设备类型”，这里把历史全局模板复制到每个
-- 已配置地区。全局模板继续保留，供管理员维护以及未来新增地区读侧兜底。
INSERT INTO public.quota_template (
    region,
    region_code,
    furnace_type_code,
    post,
    position_code,
    squad,
    squad_code,
    capacity,
    capacity2,
    capacity3,
    squadless,
    visible_roles
)
SELECT
    r.name,
    r.code,
    t.furnace_type_code,
    t.post,
    t.position_code,
    t.squad,
    t.squad_code,
    t.capacity,
    t.capacity2,
    t.capacity3,
    t.squadless,
    t.visible_roles
FROM public.quota_template AS t
CROSS JOIN public.region AS r
WHERE t.region IS NULL
  AND r.deleted = false
  AND NOT EXISTS (
      SELECT 1
      FROM public.quota_template AS existing
      WHERE existing.region = r.name
        AND existing.furnace_type_code = t.furnace_type_code
        AND existing.post = t.post
        AND existing.squad IS NOT DISTINCT FROM t.squad
  );
