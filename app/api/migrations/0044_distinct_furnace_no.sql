-- ========================================================================
-- 修复工作点编号重名：原 示例厂区A 与 示例厂区B 各有「901号工作点/902号工作点」，跨地区重名易混淆。
-- 改为四个明显不同的工作点编号（按地区定位，不依赖自增 id；记录按 furnace_id 关联，重命名不影响数据）。
--   示例厂区A 901号工作点 → 1号工作点、902号工作点 → 2号工作点
--   示例厂区B   901号工作点 → 3号工作点、902号工作点 → 4号工作点
-- 幂等：仅当旧值匹配时更新；重跑无副作用（迁移执行器本身也保证只执行一次）。
-- ========================================================================

UPDATE public.furnace SET furnace_no = '1号工作点'
    WHERE region = '示例厂区A' AND furnace_no = '901号工作点';
UPDATE public.furnace SET furnace_no = '2号工作点'
    WHERE region = '示例厂区A' AND furnace_no = '902号工作点';
UPDATE public.furnace SET furnace_no = '3号工作点'
    WHERE region = '示例厂区B' AND furnace_no = '901号工作点';
UPDATE public.furnace SET furnace_no = '4号工作点'
    WHERE region = '示例厂区B' AND furnace_no = '902号工作点';
