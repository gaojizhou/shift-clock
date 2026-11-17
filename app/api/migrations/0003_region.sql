-- ========================================================================
-- 表：region
-- 地区字典：示例厂区A / 示例厂区B。
-- 工作点、班次窗、编制模板、可选岗等的 region_code 引用本表 code。
-- 维护入口：管理后台「基础字典 → 地区」（仅 admin）。
-- ========================================================================

CREATE TABLE public.region (
    code character varying(16) NOT NULL,                    -- 地区编码，带前缀 REG（如 REG01）
    name character varying(64) NOT NULL,                    -- 地区名称（唯一）
    sort integer NOT NULL,                                  -- 列表排序值
    created_at timestamp with time zone NOT NULL,           -- 创建时间（UTC）
    updated_at timestamp with time zone NOT NULL,           -- 最后修改时间（UTC，更新时自动刷新）
    created_by character varying(64),                       -- 创建人（工号或 admin）
    updated_by character varying(64),                       -- 最后修改人（工号或 admin）
    deleted boolean NOT NULL                                -- 软删除标记：true=已删除（仅隐藏不物理删，可追溯）
);

ALTER TABLE ONLY public.region
    ADD CONSTRAINT region_name_key UNIQUE (name);

ALTER TABLE ONLY public.region
    ADD CONSTRAINT region_pkey PRIMARY KEY (code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.region IS '地区字典：示例厂区A / 示例厂区B。';
COMMENT ON COLUMN public.region.code IS '地区编码，带前缀 REG（如 REG01）';
COMMENT ON COLUMN public.region.name IS '地区名称（唯一）';
COMMENT ON COLUMN public.region.sort IS '列表排序值';
COMMENT ON COLUMN public.region.created_at IS '创建时间（UTC）';
COMMENT ON COLUMN public.region.updated_at IS '最后修改时间（UTC，更新时自动刷新）';
COMMENT ON COLUMN public.region.created_by IS '创建人（工号或 admin）';
COMMENT ON COLUMN public.region.updated_by IS '最后修改人（工号或 admin）';
COMMENT ON COLUMN public.region.deleted IS '软删除标记：true=已删除（仅隐藏不物理删，可追溯）';

-- 初始数据
INSERT INTO public.region VALUES
	('REG01', '示例厂区A', 1, '2026-06-10 14:09:42.19096+00', '2026-06-10 14:09:42.190961+00', 'admin', 'admin', false),
	('REG02', '示例厂区B', 2, '2026-06-10 14:09:42.190962+00', '2026-06-10 14:09:42.190962+00', 'admin', 'admin', false);
