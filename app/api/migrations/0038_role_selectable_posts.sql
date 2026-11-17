-- ========================================================================
-- 编制模板「可见角色」按角色规则统一配置，并应用到所有工作点。
--
-- 规则（角色 → 可选职位）：
--   值班长(chief)   ：所有职位
--   维修工(repairer)：除「值班长」外所有职位
--   班组长(leader)  ：班组长、副班组长、以及操作工职位
--   操作工(worker)：仅操作工职位（非值班长/非维修工/非正副班组长）
--
-- 等价地，按「职位 → 可见角色」落库 visible_roles（role.code 逗号拼接）：
--   值班长            → chief
--   维修工          → chief,repairer
--   班组长 / 副班组长   → chief,repairer,leader
--   其余(操作工职位)  → chief,repairer,leader,worker
--
-- 三个设备类型(FT01/FT02/FT03)的默认模板统一更新；再「应用到所有工作点」=按各工作点设备类型
-- 模板重建该工作点的 selectable_post（工作点级覆盖，扫码实际读取此表）。
-- ========================================================================

-- 1) 更新 quota_template.visible_roles（覆盖全部设备类型）
UPDATE public.quota_template SET visible_roles = 'chief'                       WHERE post = '值班长';
UPDATE public.quota_template SET visible_roles = 'chief,repairer'              WHERE post = '维修工';
UPDATE public.quota_template SET visible_roles = 'chief,repairer,leader'       WHERE post IN ('班组长', '副班组长');
UPDATE public.quota_template SET visible_roles = 'chief,repairer,leader,worker'
  WHERE post NOT IN ('值班长', '维修工', '班组长', '副班组长');

-- 2) 应用到所有工作点：重建工作点级 selectable_post（先清工作点级，再按各工作点设备类型模板写入每岗位）
DELETE FROM public.selectable_post WHERE furnace_no IS NOT NULL;

INSERT INTO public.selectable_post (region, region_code, furnace_no, post, position_code, visible_roles)
SELECT f.region,
       f.region_code,
       f.furnace_no,
       qt.post,
       (SELECT p.code FROM public."position" p WHERE p.name = qt.post),
       qt.visible_roles
FROM public.furnace f
JOIN public.quota_template qt ON qt.furnace_type_code = f.furnace_type_code
WHERE qt.visible_roles <> '';
