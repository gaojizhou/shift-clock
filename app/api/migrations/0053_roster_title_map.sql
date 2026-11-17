-- ========================================================================
-- 0053：钉钉职位 → 角色/常用职位 对照字典 roster_title_map
--
-- 用途：转花名册时按钉钉原始 title 命中本表，自动写入
--   employee.role（role.code）+ 员工「常用岗位」收藏（employee_favorite kind=post）；
--   role=chief 时再按员工钉钉部门（地区=root_label，工作点编号=部门路径末级）自动绑定工作点-值班长。
-- role/post 均可空（只映射其一）。维护入口：管理后台「通讯录同步 → 编辑职位映射」。
-- 不预置数据：HR 在页面里「导入未映射职位」后逐条配置。
-- ========================================================================

CREATE TABLE public.roster_title_map (
    id integer NOT NULL,                                    -- 自增主键
    title character varying(128) NOT NULL,                  -- 钉钉职位（原始 title，唯一）
    role character varying(16),                             -- 角色 → role.code（可空）
    post character varying(64),                             -- 常用职位（岗位名，可空）
    sort integer NOT NULL                                   -- 展示顺序
);

CREATE SEQUENCE public.roster_title_map_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.roster_title_map_id_seq OWNED BY public.roster_title_map.id;
ALTER TABLE ONLY public.roster_title_map ALTER COLUMN id SET DEFAULT nextval('public.roster_title_map_id_seq'::regclass);

ALTER TABLE ONLY public.roster_title_map
    ADD CONSTRAINT roster_title_map_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.roster_title_map
    ADD CONSTRAINT roster_title_map_title_key UNIQUE (title);

COMMENT ON TABLE public.roster_title_map IS '钉钉职位→角色/常用职位对照：转花名册时按 title 命中，写 role + 常用岗位收藏；role=chief 按部门绑值班长。';
COMMENT ON COLUMN public.roster_title_map.id IS '自增主键';
COMMENT ON COLUMN public.roster_title_map.title IS '钉钉职位（原始 title，唯一）';
COMMENT ON COLUMN public.roster_title_map.role IS '角色 → role.code（可空）';
COMMENT ON COLUMN public.roster_title_map.post IS '常用职位（岗位名，可空）';
COMMENT ON COLUMN public.roster_title_map.sort IS '展示顺序';
