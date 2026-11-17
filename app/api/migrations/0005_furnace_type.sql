-- ========================================================================
-- 表：furnace_type
-- 设备类型字典：标准型A / 标准型B / 标准型C（设备规格）。
-- 工作点与设备类型编制模板仅存本表 code（FTxx）引用，名称展示一律 join 本表（已去双轨）。
-- 维护入口：管理后台「基础字典 → 设备类型」（仅 admin）；被工作点/模板引用时禁止删除。
-- ========================================================================

CREATE TABLE public.furnace_type (
    code character varying(16) NOT NULL,                    -- 设备类型编码，带前缀 FT（如 FT01）
    name character varying(64) NOT NULL,                    -- 设备类型名称（唯一），如 标准型A
    sort integer NOT NULL,                                  -- 列表排序值
    created_at timestamp with time zone NOT NULL,           -- 创建时间（UTC）
    updated_at timestamp with time zone NOT NULL,           -- 最后修改时间（UTC，更新时自动刷新）
    created_by character varying(64),                       -- 创建人（工号或 admin）
    updated_by character varying(64),                       -- 最后修改人（工号或 admin）
    deleted boolean NOT NULL                                -- 软删除标记：true=已删除（仅隐藏不物理删，可追溯）
);

ALTER TABLE ONLY public.furnace_type
    ADD CONSTRAINT furnace_type_name_key UNIQUE (name);

ALTER TABLE ONLY public.furnace_type
    ADD CONSTRAINT furnace_type_pkey PRIMARY KEY (code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.furnace_type IS '设备类型字典：标准型A / 标准型B / 标准型C（设备规格）。';
COMMENT ON COLUMN public.furnace_type.code IS '设备类型编码，带前缀 FT（如 FT01）';
COMMENT ON COLUMN public.furnace_type.name IS '设备类型名称（唯一），如 标准型A';
COMMENT ON COLUMN public.furnace_type.sort IS '列表排序值';
COMMENT ON COLUMN public.furnace_type.created_at IS '创建时间（UTC）';
COMMENT ON COLUMN public.furnace_type.updated_at IS '最后修改时间（UTC，更新时自动刷新）';
COMMENT ON COLUMN public.furnace_type.created_by IS '创建人（工号或 admin）';
COMMENT ON COLUMN public.furnace_type.updated_by IS '最后修改人（工号或 admin）';
COMMENT ON COLUMN public.furnace_type.deleted IS '软删除标记：true=已删除（仅隐藏不物理删，可追溯）';

-- 初始数据
INSERT INTO public.furnace_type VALUES
	('FT01', '标准型A', 1, '2026-06-10 14:09:41.208185+00', '2026-06-10 14:09:41.20819+00', 'admin', 'admin', false),
	('FT02', '标准型B', 2, '2026-06-10 14:09:41.208191+00', '2026-06-10 14:09:41.208191+00', 'admin', 'admin', false),
	('FT03', '标准型C', 3, '2026-06-10 14:09:41.208192+00', '2026-06-10 14:09:41.208192+00', 'admin', 'admin', false);
