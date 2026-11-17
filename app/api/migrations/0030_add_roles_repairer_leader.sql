-- ========================================================================
-- 新增角色（工人种类）：维修工 repairer / 班组长 leader
--
-- 背景：0001 role 字典原仅 worker/chief/hr/stat/admin 五类。本迁移在「工人」
--   之外细分两类一线工人种类——维修工、班组长，作为独立角色入 role 字典。
--   注：position/post_catalog 早有「维修工/班组长」工种条目；此处加的是 role
--   （角色/工人种类），与工种字典是两个维度，互不冲突。
--
-- ⚠️ 配合代码：app/api/app/routers/api.py 的 VALID_ROLES 已同步加入
--   'repairer'/'leader'，否则在管理后台「员工管理」编辑这两类员工时，
--   角色会被校验回落为 worker。
-- ========================================================================

-- 1) role 字典新增两个工人种类（sort 接在 admin=5 之后）
INSERT INTO public.role (code, name, sort, created_at, updated_at, created_by, updated_by, deleted) VALUES
	('repairer', '维修工', 6, '2026-06-11 00:00:00+00', '2026-06-11 00:00:00+00', 'admin', 'admin', false),
	('leader',   '班组长',   7, '2026-06-11 00:00:00+00', '2026-06-11 00:00:00+00', 'admin', 'admin', false);

-- 2) 员工由管理后台或演示数据命令创建，本迁移不写入示例员工。

