-- ========================================================================
-- 工作点编号改用三位数表示（在 0044 去重命名基础上）：
--   示例厂区A 1号工作点→101号工作点、2号工作点→102号工作点
--   示例厂区B   3号工作点→201号工作点、4号工作点→202号工作点
-- 三位数 + 地区前缀(1xx=示例厂区A, 2xx=示例厂区B)，四个工作点编号明显不同。按地区定位，记录按 furnace_id 关联。
-- ========================================================================

UPDATE public.furnace SET furnace_no = '101号工作点'
    WHERE region = '示例厂区A' AND furnace_no = '1号工作点';
UPDATE public.furnace SET furnace_no = '102号工作点'
    WHERE region = '示例厂区A' AND furnace_no = '2号工作点';
UPDATE public.furnace SET furnace_no = '201号工作点'
    WHERE region = '示例厂区B' AND furnace_no = '3号工作点';
UPDATE public.furnace SET furnace_no = '202号工作点'
    WHERE region = '示例厂区B' AND furnace_no = '4号工作点';
