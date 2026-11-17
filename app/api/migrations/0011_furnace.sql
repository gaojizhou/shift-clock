-- ========================================================================
-- 表：furnace
-- 工作点主数据（工作点）。具体工作点由后台维护或演示数据命令创建。
-- 设备类型仅存 furnace_type_code（FTxx），名称展示 join furnace_type；同名工作点编号在不同地区是
-- 不同 code 的两条记录（如 示例厂区A1号工作点=FUR01、示例厂区B1号工作点=FUR53）。
-- 维护入口：管理后台「基础字典 → 工作点」（仅 admin；停用不物理删）。
-- ========================================================================

CREATE TABLE public.furnace (
    id integer NOT NULL,                                    -- 自增主键（quota_rule/scan_record 等以 furnace_id 引用）
    code character varying(16),                             -- 全局唯一工作点编码，带前缀 FUR（如 FUR01）
    region character varying(64) NOT NULL,                  -- 地区名称（展示冗余；规范引用是 region_code）
    region_code character varying(16),                      -- 地区字典外键 → region.code（REGxx）
    furnace_no character varying(32) NOT NULL,              -- 工作点编号（如 1号工作点、35号工作点），同地区内唯一
    furnace_no_order integer NOT NULL,                      -- 工作点编号中的数字（1号工作点→1），列表按它排序
    furnace_type_code character varying(16),                -- 设备类型字典外键 → furnace_type.code（FTxx）；名称一律 join 字典
    name character varying(128),                            -- 全名 = 地区+工作点编号（如 示例厂区A1号工作点），展示用
    chief_user_id character varying(64),                    -- （历史遗留）单值班长工号；现行绑定关系在 furnace_chief 表
    status character varying(32) NOT NULL                   -- 状态：active 启用 / inactive 停用（字典页停用，不物理删）
);

CREATE SEQUENCE public.furnace_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.furnace_id_seq OWNED BY public.furnace.id;

ALTER TABLE ONLY public.furnace ALTER COLUMN id SET DEFAULT nextval('public.furnace_id_seq'::regclass);

ALTER TABLE ONLY public.furnace
    ADD CONSTRAINT furnace_code_key UNIQUE (code);

ALTER TABLE ONLY public.furnace
    ADD CONSTRAINT furnace_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.furnace
    ADD CONSTRAINT furnace_region_furnace_no_key UNIQUE (region, furnace_no);

ALTER TABLE ONLY public.furnace
    ADD CONSTRAINT furnace_furnace_type_code_fkey FOREIGN KEY (furnace_type_code) REFERENCES public.furnace_type(code);

ALTER TABLE ONLY public.furnace
    ADD CONSTRAINT furnace_region_code_fkey FOREIGN KEY (region_code) REFERENCES public.region(code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.furnace IS '工作点/工作点主数据。';
COMMENT ON COLUMN public.furnace.id IS '自增主键（quota_rule/scan_record 等以 furnace_id 引用）';
COMMENT ON COLUMN public.furnace.code IS '全局唯一工作点编码，带前缀 FUR（如 FUR01）';
COMMENT ON COLUMN public.furnace.region IS '地区名称（展示冗余；规范引用是 region_code）';
COMMENT ON COLUMN public.furnace.region_code IS '地区字典外键 → region.code（REGxx）';
COMMENT ON COLUMN public.furnace.furnace_no IS '工作点编号（如 1号工作点、35号工作点），同地区内唯一';
COMMENT ON COLUMN public.furnace.furnace_no_order IS '工作点编号中的数字（1号工作点→1），列表按它排序';
COMMENT ON COLUMN public.furnace.furnace_type_code IS '设备类型字典外键 → furnace_type.code（FTxx）；名称一律 join 字典';
COMMENT ON COLUMN public.furnace.name IS '全名 = 地区+工作点编号（如 示例厂区A1号工作点），展示用';
COMMENT ON COLUMN public.furnace.chief_user_id IS '（历史遗留）单值班长工号；现行绑定关系在 furnace_chief 表';
COMMENT ON COLUMN public.furnace.status IS '状态：active 启用 / inactive 停用（字典页停用，不物理删）';

-- 初始数据：无。演示数据由 `python -m app.seed` 显式载入。

