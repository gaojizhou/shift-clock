-- ========================================================================
-- 表：quota_template
-- 设备类型编制模板：按设备类型存每岗一行，批量应用时展开成各工作点的 quota_rule。
-- squadless 岗（值班长/维修工）capacity 即该岗总人数；分班岗 capacity/2/3 班可不同
-- （capacity2/3 为 NULL 表示与 1 班联动同值）。维护入口：工作点编制页「设备类型编制模板（批量）」。
-- ========================================================================

CREATE TABLE public.quota_template (
    id integer NOT NULL,                                    -- 自增主键
    region character varying(64),                           -- 地区限定（NULL=全局通用模板；预留字段）
    region_code character varying(16),                      -- 地区字典外键 → region.code（预留）
    furnace_type_code character varying(16),                -- 设备类型字典外键 → furnace_type.code（FTxx）；模板按设备类型分组
    post character varying(64) NOT NULL,                    -- 岗位名称（展示冗余）
    position_code character varying(16),                    -- 职位字典外键 → position.code
    squad character varying(16),                            -- 恒 NULL（模板只存单班值，应用时展开 1/2/3 班）
    squad_code character varying(16),                       -- 恒 NULL（同上）
    capacity integer NOT NULL,                              -- 1 班人数（不分班岗=该岗总人数）
    capacity2 integer,                                      -- 2 班人数；NULL=与 1 班联动同值
    capacity3 integer,                                      -- 3 班人数；NULL=与 1 班联动同值
    squadless boolean NOT NULL,                             -- true=不分班岗位（值班长/维修工）
    selectable boolean NOT NULL                             -- 该岗是否「扫码可选」（批量应用时写入 selectable_post）
);

CREATE SEQUENCE public.quota_template_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.quota_template_id_seq OWNED BY public.quota_template.id;

ALTER TABLE ONLY public.quota_template ALTER COLUMN id SET DEFAULT nextval('public.quota_template_id_seq'::regclass);

ALTER TABLE ONLY public.quota_template
    ADD CONSTRAINT quota_template_furnace_type_code_post_squad_key UNIQUE (furnace_type_code, post, squad);

ALTER TABLE ONLY public.quota_template
    ADD CONSTRAINT quota_template_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.quota_template
    ADD CONSTRAINT quota_template_furnace_type_code_fkey FOREIGN KEY (furnace_type_code) REFERENCES public.furnace_type(code);

ALTER TABLE ONLY public.quota_template
    ADD CONSTRAINT quota_template_position_code_fkey FOREIGN KEY (position_code) REFERENCES public."position"(code);

ALTER TABLE ONLY public.quota_template
    ADD CONSTRAINT quota_template_region_code_fkey FOREIGN KEY (region_code) REFERENCES public.region(code);

ALTER TABLE ONLY public.quota_template
    ADD CONSTRAINT quota_template_squad_code_fkey FOREIGN KEY (squad_code) REFERENCES public.squad(code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.quota_template IS '设备类型编制模板：按设备类型存每岗一行，批量应用时展开成各工作点的 quota_rule。';
COMMENT ON COLUMN public.quota_template.id IS '自增主键';
COMMENT ON COLUMN public.quota_template.region IS '地区限定（NULL=全局通用模板；预留字段）';
COMMENT ON COLUMN public.quota_template.region_code IS '地区字典外键 → region.code（预留）';
COMMENT ON COLUMN public.quota_template.furnace_type_code IS '设备类型字典外键 → furnace_type.code（FTxx）；模板按设备类型分组';
COMMENT ON COLUMN public.quota_template.post IS '岗位名称（展示冗余）';
COMMENT ON COLUMN public.quota_template.position_code IS '职位字典外键 → position.code';
COMMENT ON COLUMN public.quota_template.squad IS '恒 NULL（模板只存单班值，应用时展开 1/2/3 班）';
COMMENT ON COLUMN public.quota_template.squad_code IS '恒 NULL（同上）';
COMMENT ON COLUMN public.quota_template.capacity IS '1 班人数（不分班岗=该岗总人数）';
COMMENT ON COLUMN public.quota_template.capacity2 IS '2 班人数；NULL=与 1 班联动同值';
COMMENT ON COLUMN public.quota_template.capacity3 IS '3 班人数；NULL=与 1 班联动同值';
COMMENT ON COLUMN public.quota_template.squadless IS 'true=不分班岗位（值班长/维修工）';
COMMENT ON COLUMN public.quota_template.selectable IS '该岗是否「扫码可选」（批量应用时写入 selectable_post）';

-- 初始数据
INSERT INTO public.quota_template VALUES
	(1, NULL, NULL, 'FT01', '值班长', 'POS01', NULL, NULL, 1, 1, 1, true, false),
	(2, NULL, NULL, 'FT01', '维修工', 'POS02', NULL, NULL, 2, 2, 2, true, false),
	(3, NULL, NULL, 'FT01', '班组长', 'POS03', NULL, NULL, 1, 1, 1, false, true),
	(4, NULL, NULL, 'FT01', '副班组长', 'POS04', NULL, NULL, 1, 1, 1, false, true),
	(5, NULL, NULL, 'FT01', '物料转运工', 'POS05', NULL, NULL, 1, 1, 1, false, true),
	(6, NULL, NULL, 'FT01', '加料操作工', 'POS06', NULL, NULL, 1, 1, 1, false, true),
	(7, NULL, NULL, 'FT01', '出料工', 'POS07', NULL, NULL, 1, 1, 1, false, true),
	(8, NULL, NULL, 'FT01', '供气工', 'POS08', NULL, NULL, 1, 1, 1, false, true),
	(9, NULL, NULL, 'FT02', '值班长', 'POS01', NULL, NULL, 1, 1, 1, true, false),
	(10, NULL, NULL, 'FT02', '维修工', 'POS02', NULL, NULL, 2, 2, 2, true, false),
	(11, NULL, NULL, 'FT02', '班组长', 'POS03', NULL, NULL, 1, 1, 1, false, true),
	(12, NULL, NULL, 'FT02', '副班组长', 'POS04', NULL, NULL, 1, 1, 1, false, true),
	(13, NULL, NULL, 'FT02', '物料转运工', 'POS05', NULL, NULL, 1, 1, 1, false, true),
	(14, NULL, NULL, 'FT02', '加料操作工', 'POS06', NULL, NULL, 1, 1, 1, false, true),
	(15, NULL, NULL, 'FT02', '出料工', 'POS07', NULL, NULL, 2, 2, 2, false, true),
	(16, NULL, NULL, 'FT02', '供气工', 'POS08', NULL, NULL, 1, 1, 1, false, true),
	(17, NULL, NULL, 'FT03', '值班长', 'POS01', NULL, NULL, 1, 1, 1, true, false),
	(18, NULL, NULL, 'FT03', '维修工', 'POS02', NULL, NULL, 2, 2, 2, true, false),
	(19, NULL, NULL, 'FT03', '班组长', 'POS03', NULL, NULL, 1, 1, 1, false, true),
	(20, NULL, NULL, 'FT03', '副班组长', 'POS04', NULL, NULL, 1, 1, 1, false, true),
	(21, NULL, NULL, 'FT03', '物料工', 'POS09', NULL, NULL, 3, 3, 3, false, true),
	(22, NULL, NULL, 'FT03', '出料工', 'POS07', NULL, NULL, 2, 2, 2, false, true),
	(23, NULL, NULL, 'FT03', '供气工', 'POS08', NULL, NULL, 1, 1, 1, false, true);

SELECT pg_catalog.setval('public.quota_template_id_seq', 23, true);
