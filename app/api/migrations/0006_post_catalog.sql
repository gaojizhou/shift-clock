-- ========================================================================
-- 表：post_catalog
-- 岗位候选字典：编制编辑页「岗位」下拉的可选项全集。
-- 与 position 字典的区别：本表是编制编辑时可白名单新增的轻量候选集（HR 在
-- 工作点编制页「岗位字典」子页维护），position 是带 POS 编码的正式字典。
-- ========================================================================

CREATE TABLE public.post_catalog (
    id integer NOT NULL,                                    -- 自增主键
    name character varying(64) NOT NULL,                    -- 岗位名称（唯一）
    deleted boolean NOT NULL                                -- 软删除标记：true=已删除（下拉不再出现）
);

CREATE SEQUENCE public.post_catalog_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.post_catalog_id_seq OWNED BY public.post_catalog.id;

ALTER TABLE ONLY public.post_catalog ALTER COLUMN id SET DEFAULT nextval('public.post_catalog_id_seq'::regclass);

ALTER TABLE ONLY public.post_catalog
    ADD CONSTRAINT post_catalog_name_key UNIQUE (name);

ALTER TABLE ONLY public.post_catalog
    ADD CONSTRAINT post_catalog_pkey PRIMARY KEY (id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.post_catalog IS '岗位候选字典：编制编辑页「岗位」下拉的可选项全集。';
COMMENT ON COLUMN public.post_catalog.id IS '自增主键';
COMMENT ON COLUMN public.post_catalog.name IS '岗位名称（唯一）';
COMMENT ON COLUMN public.post_catalog.deleted IS '软删除标记：true=已删除（下拉不再出现）';

-- 初始数据
INSERT INTO public.post_catalog VALUES
	(1, '值班长', false),
	(2, '维修工', false),
	(3, '班组长', false),
	(4, '副班组长', false),
	(5, '物料转运工', false),
	(6, '加料操作工', false),
	(7, '出料工', false),
	(8, '供气工', false),
	(9, '物料工', false);

SELECT pg_catalog.setval('public.post_catalog_id_seq', 9, true);
