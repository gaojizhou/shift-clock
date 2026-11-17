-- ========================================================================
-- 表：selectable_post
-- 扫码可选岗白名单：工人扫码上岗时可选择的岗位。
-- 两级：furnace_no=NULL 为地区级默认；指定 furnace_no 为工作点级覆盖（模板批量应用/单工作点
-- 编辑时重写）。读取逻辑：工作点级有记录用工作点级，否则用地区级。
-- ========================================================================

CREATE TABLE public.selectable_post (
    id integer NOT NULL,                                    -- 自增主键
    region character varying(64) NOT NULL,                  -- 地区名称（展示冗余）
    region_code character varying(16),                      -- 地区字典外键 → region.code
    furnace_no character varying(32),                       -- 工作点编号；NULL=地区级默认，非 NULL=该工作点级覆盖
    post character varying(64) NOT NULL,                    -- 岗位名称
    position_code character varying(16)                     -- 职位字典外键 → position.code
);

CREATE SEQUENCE public.selectable_post_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.selectable_post_id_seq OWNED BY public.selectable_post.id;

ALTER TABLE ONLY public.selectable_post ALTER COLUMN id SET DEFAULT nextval('public.selectable_post_id_seq'::regclass);

ALTER TABLE ONLY public.selectable_post
    ADD CONSTRAINT selectable_post_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.selectable_post
    ADD CONSTRAINT selectable_post_region_furnace_no_post_key UNIQUE (region, furnace_no, post);

ALTER TABLE ONLY public.selectable_post
    ADD CONSTRAINT selectable_post_position_code_fkey FOREIGN KEY (position_code) REFERENCES public."position"(code);

ALTER TABLE ONLY public.selectable_post
    ADD CONSTRAINT selectable_post_region_code_fkey FOREIGN KEY (region_code) REFERENCES public.region(code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.selectable_post IS '扫码可选岗白名单：工人扫码上岗时可选择的岗位。';
COMMENT ON COLUMN public.selectable_post.id IS '自增主键';
COMMENT ON COLUMN public.selectable_post.region IS '地区名称（展示冗余）';
COMMENT ON COLUMN public.selectable_post.region_code IS '地区字典外键 → region.code';
COMMENT ON COLUMN public.selectable_post.furnace_no IS '工作点编号；NULL=地区级默认，非 NULL=该工作点级覆盖';
COMMENT ON COLUMN public.selectable_post.post IS '岗位名称';
COMMENT ON COLUMN public.selectable_post.position_code IS '职位字典外键 → position.code';

-- 初始数据
INSERT INTO public.selectable_post VALUES
	(1, '示例厂区B', 'REG02', NULL, '出料工', 'POS07'),
	(2, '示例厂区B', 'REG02', NULL, '供气工', 'POS08'),
	(3, '示例厂区B', 'REG02', NULL, '物料转运工', 'POS05'),
	(4, '示例厂区B', 'REG02', NULL, '加料操作工', 'POS06'),
	(5, '示例厂区B', 'REG02', NULL, '物料工', 'POS09'),
	(6, '示例厂区A', 'REG01', NULL, '出料工', 'POS07'),
	(7, '示例厂区A', 'REG01', NULL, '供气工', 'POS08'),
	(8, '示例厂区A', 'REG01', NULL, '物料转运工', 'POS05'),
	(9, '示例厂区A', 'REG01', NULL, '加料操作工', 'POS06'),
	(10, '示例厂区A', 'REG01', NULL, '物料工', 'POS09');

SELECT pg_catalog.setval('public.selectable_post_id_seq', 10, true);
