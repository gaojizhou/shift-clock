-- ========================================================================
-- 表：role
-- 角色字典：worker 操作工 / chief 值班长 / hr 人事专员 / stat 统计员 / admin 系统管理员。
-- employee.role 引用本表 code。admin 为超级权限（放行全部受角色限制的接口）。
-- 维护入口：管理后台「基础字典 → 角色」（仅 admin）。
-- ========================================================================

CREATE TABLE public.role (
    code character varying(16) NOT NULL,                    -- 角色编码=业务唯一ID：worker/chief/hr/stat/admin
    name character varying(32) NOT NULL,                    -- 角色中文名：操作工/值班长/人事专员/统计员/系统管理员
    sort integer NOT NULL,                                  -- 列表排序值（小在前）
    created_at timestamp with time zone NOT NULL,           -- 创建时间（UTC）
    updated_at timestamp with time zone NOT NULL,           -- 最后修改时间（UTC，更新时自动刷新）
    created_by character varying(64),                       -- 创建人（工号或 admin）
    updated_by character varying(64),                       -- 最后修改人（工号或 admin）
    deleted boolean NOT NULL                                -- 软删除标记：true=已删除（仅隐藏不物理删，可追溯）
);

ALTER TABLE ONLY public.role
    ADD CONSTRAINT role_pkey PRIMARY KEY (code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.role IS '角色字典：worker 操作工 / chief 值班长 / hr 人事专员 / stat 统计员 / admin 系统管理员。';
COMMENT ON COLUMN public.role.code IS '角色编码=业务唯一ID：worker/chief/hr/stat/admin';
COMMENT ON COLUMN public.role.name IS '角色中文名：工人/值班长/人事/统计/管理员';
COMMENT ON COLUMN public.role.sort IS '列表排序值（小在前）';
COMMENT ON COLUMN public.role.created_at IS '创建时间（UTC）';
COMMENT ON COLUMN public.role.updated_at IS '最后修改时间（UTC，更新时自动刷新）';
COMMENT ON COLUMN public.role.created_by IS '创建人（工号或 admin）';
COMMENT ON COLUMN public.role.updated_by IS '最后修改人（工号或 admin）';
COMMENT ON COLUMN public.role.deleted IS '软删除标记：true=已删除（仅隐藏不物理删，可追溯）';

-- 初始数据
INSERT INTO public.role VALUES
	('worker', '操作工', 1, '2026-06-10 14:09:42.191419+00', '2026-06-10 14:09:42.19142+00', 'admin', 'admin', false),
	('chief', '值班长', 2, '2026-06-10 14:09:42.19142+00', '2026-06-10 14:09:42.191421+00', 'admin', 'admin', false),
	('hr', '人事专员', 3, '2026-06-10 14:09:42.191421+00', '2026-06-10 14:09:42.191421+00', 'admin', 'admin', false),
	('stat', '统计员', 4, '2026-06-10 14:09:42.191422+00', '2026-06-10 14:09:42.191422+00', 'admin', 'admin', false),
	('admin', '系统管理员', 5, '2026-06-10 14:09:42.191423+00', '2026-06-10 14:09:42.191423+00', 'admin', 'admin', false);
