-- ========================================================================
-- 0051：设备类型编制模板扩展（开源版已移除客户工作点清单）
--
-- 本迁移内容：
--   1) 新增 4 个设备类型字典（FT04~FT07），并按设备规格重排 sort；
--   2) 为 4 个新设备类型写入设备类型编制模板 quota_template；
--   3) 按现有工作点 + 设备类型模板生成工作点编制 quota_rule 与扫码可选岗 selectable_post；
--      新库无工作点时这些语句为空操作，演示工作点由 `python -m app.seed` 创建。
--
-- 设备类型→编制（单班）：
--   标准型A/D/E/F（共21人）：值班长1·维修工2 + [班组长/副班组长/物料转运工/加料操作工/出料工/供气工]各1×3班
--   标准型B/G（共24人）：同上，出料工 2/班
--   标准型C（共27人）：值班长1·维修工2 + [班组长1/副班组长1/物料工3/出料工2/供气工1]×3班（FT03，已存在）
-- 注：本文件不写 BEGIN/COMMIT —— 迁移执行器(app/migrate.py)已将每个文件包在单个事务里，
--     失败整体回滚且不记账（与 0001~0050 约定一致）。
-- ========================================================================

-- 1) 设备类型字典：新增 4 型 + 按设备规格升序重排 sort
UPDATE public.furnace_type SET sort = 1 WHERE code = 'FT01';  -- 标准型A
UPDATE public.furnace_type SET sort = 5 WHERE code = 'FT02';  -- 标准型B
UPDATE public.furnace_type SET sort = 7 WHERE code = 'FT03';  -- 标准型C
INSERT INTO public.furnace_type (code, name, sort, created_at, updated_at, created_by, updated_by, deleted) VALUES
	('FT04', '标准型D', 2, '2026-06-23 00:00:00+00', '2026-06-23 00:00:00+00', 'admin', 'admin', false),
	('FT05', '标准型E', 3, '2026-06-23 00:00:00+00', '2026-06-23 00:00:00+00', 'admin', 'admin', false),
	('FT06', '标准型F', 4, '2026-06-23 00:00:00+00', '2026-06-23 00:00:00+00', 'admin', 'admin', false),
	('FT07', '标准型G', 6, '2026-06-23 00:00:00+00', '2026-06-23 00:00:00+00', 'admin', 'admin', false)
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, sort = EXCLUDED.sort, updated_at = EXCLUDED.updated_at, deleted = false;

-- 2) 设备类型编制模板：4 个新设备类型（squad/region 恒 NULL；capacity2/3 与 1 班同值）
INSERT INTO public.quota_template
  (region, region_code, furnace_type_code, post, position_code, squad, squad_code, capacity, capacity2, capacity3, squadless, visible_roles) VALUES
	(NULL, NULL, 'FT04', '值班长', 'POS01', NULL, NULL, 1, 1, 1, true, 'chief'),
	(NULL, NULL, 'FT04', '维修工', 'POS02', NULL, NULL, 2, 2, 2, true, 'chief,repairer'),
	(NULL, NULL, 'FT04', '班组长', 'POS03', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader'),
	(NULL, NULL, 'FT04', '副班组长', 'POS04', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader'),
	(NULL, NULL, 'FT04', '物料转运工', 'POS05', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT04', '加料操作工', 'POS06', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT04', '出料工', 'POS07', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT04', '供气工', 'POS08', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT05', '值班长', 'POS01', NULL, NULL, 1, 1, 1, true, 'chief'),
	(NULL, NULL, 'FT05', '维修工', 'POS02', NULL, NULL, 2, 2, 2, true, 'chief,repairer'),
	(NULL, NULL, 'FT05', '班组长', 'POS03', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader'),
	(NULL, NULL, 'FT05', '副班组长', 'POS04', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader'),
	(NULL, NULL, 'FT05', '物料转运工', 'POS05', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT05', '加料操作工', 'POS06', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT05', '出料工', 'POS07', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT05', '供气工', 'POS08', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT06', '值班长', 'POS01', NULL, NULL, 1, 1, 1, true, 'chief'),
	(NULL, NULL, 'FT06', '维修工', 'POS02', NULL, NULL, 2, 2, 2, true, 'chief,repairer'),
	(NULL, NULL, 'FT06', '班组长', 'POS03', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader'),
	(NULL, NULL, 'FT06', '副班组长', 'POS04', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader'),
	(NULL, NULL, 'FT06', '物料转运工', 'POS05', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT06', '加料操作工', 'POS06', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT06', '出料工', 'POS07', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT06', '供气工', 'POS08', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT07', '值班长', 'POS01', NULL, NULL, 1, 1, 1, true, 'chief'),
	(NULL, NULL, 'FT07', '维修工', 'POS02', NULL, NULL, 2, 2, 2, true, 'chief,repairer'),
	(NULL, NULL, 'FT07', '班组长', 'POS03', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader'),
	(NULL, NULL, 'FT07', '副班组长', 'POS04', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader'),
	(NULL, NULL, 'FT07', '物料转运工', 'POS05', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT07', '加料操作工', 'POS06', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT07', '出料工', 'POS07', NULL, NULL, 2, 2, 2, false, 'chief,repairer,leader,worker'),
	(NULL, NULL, 'FT07', '供气工', 'POS08', NULL, NULL, 1, 1, 1, false, 'chief,repairer,leader,worker')
ON CONFLICT (furnace_type_code, post, squad) DO UPDATE SET
  position_code = EXCLUDED.position_code, capacity = EXCLUDED.capacity,
  capacity2 = EXCLUDED.capacity2, capacity3 = EXCLUDED.capacity3,
  squadless = EXCLUDED.squadless, visible_roles = EXCLUDED.visible_roles;

-- 3) 客户工作点清单已在开源版移除。工作点由后台维护或演示数据命令创建。


-- 4) 工作点编制 quota_rule：按现有工作点覆盖式重建
DELETE FROM public.quota_rule
 WHERE furnace_id IN (SELECT id FROM public.furnace WHERE furnace_no_order < 900);

-- 4a) 不分班岗（值班长/维修工）：每岗 1 行 squad=NULL
INSERT INTO public.quota_rule (furnace_id, squad, squad_code, post, position_code, capacity)
SELECT f.id, NULL, NULL, qt.post, qt.position_code, qt.capacity
FROM public.furnace f
JOIN public.quota_template qt ON qt.furnace_type_code = f.furnace_type_code
WHERE f.furnace_no_order < 900 AND qt.squadless = true;

-- 4b) 分班岗：展开 1/2/3 班（capacity2/3 为 NULL 时与 1 班联动同值）
INSERT INTO public.quota_rule (furnace_id, squad, squad_code, post, position_code, capacity)
SELECT f.id, s.name, s.code, qt.post, qt.position_code,
       CASE s.code WHEN 'SQ1' THEN qt.capacity
                   WHEN 'SQ2' THEN COALESCE(qt.capacity2, qt.capacity)
                   WHEN 'SQ3' THEN COALESCE(qt.capacity3, qt.capacity) END
FROM public.furnace f
JOIN public.quota_template qt ON qt.furnace_type_code = f.furnace_type_code
JOIN public.squad s ON s.code IN ('SQ1', 'SQ2', 'SQ3')
WHERE f.furnace_no_order < 900 AND qt.squadless = false;

-- 5) 扫码可选岗 selectable_post：按现有工作点覆盖式重建（按设备类型模板 visible_roles）
DELETE FROM public.selectable_post
 WHERE furnace_no IN (SELECT furnace_no FROM public.furnace WHERE furnace_no_order < 900)
   AND region IN (SELECT region FROM public.furnace WHERE furnace_no_order < 900);

INSERT INTO public.selectable_post (region, region_code, furnace_no, post, position_code, visible_roles)
SELECT f.region, f.region_code, f.furnace_no, qt.post, qt.position_code, qt.visible_roles
FROM public.furnace f
JOIN public.quota_template qt ON qt.furnace_type_code = f.furnace_type_code
WHERE f.furnace_no_order < 900 AND qt.visible_roles <> ''
ON CONFLICT (region, furnace_no, post) DO UPDATE SET
  visible_roles = EXCLUDED.visible_roles, position_code = EXCLUDED.position_code;
