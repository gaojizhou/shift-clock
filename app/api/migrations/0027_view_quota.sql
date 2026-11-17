-- ========================================================================
-- 视图：quota（编制查询视图）
-- 功能：把 quota_rule（按 furnace_id 存储）展开为 地区/工作点编号/班次/岗位/人数 的
--       扁平视图，薪资侧与报表按业务键直接查询，无需了解内部主键。
-- 依赖：furnace + quota_rule（须在其后执行，故排在最后）。
-- 注意：视图不存数据；DROP 底层表前须先 DROP 本视图。
-- ========================================================================

CREATE VIEW public.quota AS
SELECT f.region,                -- 地区名称
       f.furnace_no AS furnace, -- 工作点编号（如 1号工作点）
       qr.squad,                -- 班次：1班/2班/3班；NULL=不分班岗
       qr.post,                 -- 岗位名称
       qr.capacity              -- 编制人数
FROM public.quota_rule qr
JOIN public.furnace f ON f.id = qr.furnace_id;

COMMENT ON VIEW public.quota IS '编制扁平视图：地区/工作点编号/班次/岗位/人数（薪资与报表用）';
