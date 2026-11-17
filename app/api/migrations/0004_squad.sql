-- ========================================================================
-- 表：squad
-- 班次字典：1班 / 2班 / 3班（倒班体系）。
-- 编制/扫码/补录等的 squad_code 引用本表 code；squad=NULL 表示不分班岗位（值班长/维修工）。
-- 维护入口：管理后台「基础字典 → 班次」（仅 admin）。
-- ========================================================================

CREATE TABLE public.squad (
    code character varying(16) NOT NULL,                    -- 班次编码，带前缀 SQ（如 SQ1）
    name character varying(16) NOT NULL,                    -- 班次名称：1班/2班/3班（唯一）
    sort integer NOT NULL,                                  -- 列表排序值
    created_at timestamp with time zone NOT NULL,           -- 创建时间（UTC）
    updated_at timestamp with time zone NOT NULL,           -- 最后修改时间（UTC，更新时自动刷新）
    created_by character varying(64),                       -- 创建人（工号或 admin）
    updated_by character varying(64),                       -- 最后修改人（工号或 admin）
    deleted boolean NOT NULL                                -- 软删除标记：true=已删除（仅隐藏不物理删，可追溯）
);

ALTER TABLE ONLY public.squad
    ADD CONSTRAINT squad_name_key UNIQUE (name);

ALTER TABLE ONLY public.squad
    ADD CONSTRAINT squad_pkey PRIMARY KEY (code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.squad IS '班次字典：1班 / 2班 / 3班（倒班体系）。';
COMMENT ON COLUMN public.squad.code IS '班次编码，带前缀 SQ（如 SQ1）';
COMMENT ON COLUMN public.squad.name IS '班次名称：1班/2班/3班（唯一）';
COMMENT ON COLUMN public.squad.sort IS '列表排序值';
COMMENT ON COLUMN public.squad.created_at IS '创建时间（UTC）';
COMMENT ON COLUMN public.squad.updated_at IS '最后修改时间（UTC，更新时自动刷新）';
COMMENT ON COLUMN public.squad.created_by IS '创建人（工号或 admin）';
COMMENT ON COLUMN public.squad.updated_by IS '最后修改人（工号或 admin）';
COMMENT ON COLUMN public.squad.deleted IS '软删除标记：true=已删除（仅隐藏不物理删，可追溯）';

-- 初始数据
INSERT INTO public.squad VALUES
	('SQ1', '1班', 1, '2026-06-10 14:09:42.191974+00', '2026-06-10 14:09:42.191975+00', 'admin', 'admin', false),
	('SQ2', '2班', 2, '2026-06-10 14:09:42.191975+00', '2026-06-10 14:09:42.191976+00', 'admin', 'admin', false),
	('SQ3', '3班', 3, '2026-06-10 14:09:42.191976+00', '2026-06-10 14:09:42.191976+00', 'admin', 'admin', false);
