-- ========================================================================
-- 表：position
-- 职位（岗位）字典：值班长/维修工/班组长/副班组长/物料转运工/加料操作工/出料工/供气工/物料工…
-- 业务表（编制/扫码/补录等）的 position_code 引用本表 code，名称列为展示冗余。
-- 维护入口：管理后台「基础字典 → 职位」（仅 admin）。
-- ========================================================================

CREATE TABLE public."position" (
    code character varying(16) NOT NULL,                    -- 职位编码，带前缀 POS（如 POS01）
    name character varying(64) NOT NULL,                    -- 职位名称（唯一）
    sort integer NOT NULL,                                  -- 列表排序值
    created_at timestamp with time zone NOT NULL,           -- 创建时间（UTC）
    updated_at timestamp with time zone NOT NULL,           -- 最后修改时间（UTC，更新时自动刷新）
    created_by character varying(64),                       -- 创建人（工号或 admin）
    updated_by character varying(64),                       -- 最后修改人（工号或 admin）
    deleted boolean NOT NULL                                -- 软删除标记：true=已删除（仅隐藏不物理删，可追溯）
);

ALTER TABLE ONLY public."position"
    ADD CONSTRAINT position_name_key UNIQUE (name);

ALTER TABLE ONLY public."position"
    ADD CONSTRAINT position_pkey PRIMARY KEY (code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public."position" IS '职位（岗位）字典：值班长/维修工/班组长/副班组长/物料转运工/加料操作工/出料工/供气工/物料工…';
COMMENT ON COLUMN public."position".code IS '职位编码，带前缀 POS（如 POS01）';
COMMENT ON COLUMN public."position".name IS '职位名称（唯一）';
COMMENT ON COLUMN public."position".sort IS '列表排序值';
COMMENT ON COLUMN public."position".created_at IS '创建时间（UTC）';
COMMENT ON COLUMN public."position".updated_at IS '最后修改时间（UTC，更新时自动刷新）';
COMMENT ON COLUMN public."position".created_by IS '创建人（工号或 admin）';
COMMENT ON COLUMN public."position".updated_by IS '最后修改人（工号或 admin）';
COMMENT ON COLUMN public."position".deleted IS '软删除标记：true=已删除（仅隐藏不物理删，可追溯）';

-- 初始数据
INSERT INTO public."position" VALUES
	('POS01', '值班长', 1, '2026-06-10 14:09:42.190224+00', '2026-06-10 14:09:42.190226+00', 'admin', 'admin', false),
	('POS02', '维修工', 2, '2026-06-10 14:09:42.190226+00', '2026-06-10 14:09:42.190227+00', 'admin', 'admin', false),
	('POS03', '班组长', 3, '2026-06-10 14:09:42.190227+00', '2026-06-10 14:09:42.190227+00', 'admin', 'admin', false),
	('POS04', '副班组长', 4, '2026-06-10 14:09:42.190228+00', '2026-06-10 14:09:42.190228+00', 'admin', 'admin', false),
	('POS05', '物料转运工', 5, '2026-06-10 14:09:42.190228+00', '2026-06-10 14:09:42.190229+00', 'admin', 'admin', false),
	('POS06', '加料操作工', 6, '2026-06-10 14:09:42.190229+00', '2026-06-10 14:09:42.190229+00', 'admin', 'admin', false),
	('POS07', '出料工', 7, '2026-06-10 14:09:42.19023+00', '2026-06-10 14:09:42.19023+00', 'admin', 'admin', false),
	('POS08', '供气工', 8, '2026-06-10 14:09:42.190231+00', '2026-06-10 14:09:42.190249+00', 'admin', 'admin', false),
	('POS09', '物料工', 9, '2026-06-10 14:09:42.190249+00', '2026-06-10 14:09:42.190249+00', 'admin', 'admin', false);
