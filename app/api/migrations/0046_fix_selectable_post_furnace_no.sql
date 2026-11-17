-- ========================================================================
-- 修复 0044/0045 工作点编号重命名的遗漏：selectable_post 以 (region, furnace_no) 为键（非 furnace_id），
-- 工作点编号改名时未联动，导致各工作点「可选岗位」配置失联（get_selectable_posts 按新工作点编号查不到工作点级配置）。
-- 这里把 selectable_post 的旧工作点编号同步为新工作点编号（与 furnace 表一致）。
--   示例厂区A 901号工作点→101号工作点、902号工作点→102号工作点
--   示例厂区B   901号工作点→201号工作点、902号工作点→202号工作点
-- （furnace_config_update 已对未来的地区/工作点编号编辑做同样联动，此迁移仅修存量。）
-- ========================================================================

UPDATE public.selectable_post SET furnace_no = '101号工作点'
    WHERE region = '示例厂区A' AND furnace_no = '901号工作点';
UPDATE public.selectable_post SET furnace_no = '102号工作点'
    WHERE region = '示例厂区A' AND furnace_no = '902号工作点';
UPDATE public.selectable_post SET furnace_no = '201号工作点'
    WHERE region = '示例厂区B' AND furnace_no = '901号工作点';
UPDATE public.selectable_post SET furnace_no = '202号工作点'
    WHERE region = '示例厂区B' AND furnace_no = '902号工作点';
