-- ========================================================================
-- 0052：钉钉通讯录同步范围（根部门）改为可在「通讯录同步」页编辑
--
-- 背景：同步范围由硬编码常量改为可维护的表 roster_root_dept，
--   HR 在页面里可改（左=描述 label，右=钉钉部门 dept_id）。
-- 服务读取顺序：表非空→按 sort,id 读取；表为空→回退代码里的 DEFAULT_ROSTER_ROOT_DEPTS。
-- ========================================================================

CREATE TABLE public.roster_root_dept (
    id integer NOT NULL,                                    -- 自增主键
    label character varying(128) NOT NULL,                  -- 描述（展示名，唯一）
    dept_id bigint NOT NULL,                                -- 钉钉部门 ID（根部门）
    sort integer NOT NULL                                   -- 迭代/展示顺序
);

CREATE SEQUENCE public.roster_root_dept_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.roster_root_dept_id_seq OWNED BY public.roster_root_dept.id;
ALTER TABLE ONLY public.roster_root_dept ALTER COLUMN id SET DEFAULT nextval('public.roster_root_dept_id_seq'::regclass);

ALTER TABLE ONLY public.roster_root_dept
    ADD CONSTRAINT roster_root_dept_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.roster_root_dept
    ADD CONSTRAINT roster_root_dept_label_key UNIQUE (label);

COMMENT ON TABLE public.roster_root_dept IS '钉钉通讯录同步范围（根部门）：描述 label → 钉钉部门 dept_id；HR 在「通讯录同步」页维护。';
COMMENT ON COLUMN public.roster_root_dept.id IS '自增主键';
COMMENT ON COLUMN public.roster_root_dept.label IS '描述（展示名，唯一）';
COMMENT ON COLUMN public.roster_root_dept.dept_id IS '钉钉部门 ID（根部门）';
COMMENT ON COLUMN public.roster_root_dept.sort IS '迭代/展示顺序';

-- 初始数据：空。同步范围由 HR 在「通讯录同步」页维护；为空时回退代码默认值。

