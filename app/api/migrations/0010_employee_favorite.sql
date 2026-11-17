-- ========================================================================
-- 表：employee_favorite
-- 员工常用项：常用地区/常用工作点/常用职位（HR 在员工管理里配置）。
-- H5 扫码上岗等场景用于优先展示。初始为空，运行期维护。
-- ========================================================================

CREATE TABLE public.employee_favorite (
    id integer NOT NULL,                                    -- 自增主键
    emp_id character varying(32) NOT NULL,                  -- 员工工号（employee.emp_id）
    kind character varying(16) NOT NULL,                    -- 类型：region 地区 / furnace 工作点 / post 职位
    value character varying(64) NOT NULL                    -- 对应取值（地区名/工作点编号/职位名），同一员工同类型可多行
);

CREATE SEQUENCE public.employee_favorite_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.employee_favorite_id_seq OWNED BY public.employee_favorite.id;

ALTER TABLE ONLY public.employee_favorite ALTER COLUMN id SET DEFAULT nextval('public.employee_favorite_id_seq'::regclass);

ALTER TABLE ONLY public.employee_favorite
    ADD CONSTRAINT employee_favorite_emp_id_kind_value_key UNIQUE (emp_id, kind, value);

ALTER TABLE ONLY public.employee_favorite
    ADD CONSTRAINT employee_favorite_pkey PRIMARY KEY (id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.employee_favorite IS '员工常用项：常用地区/常用工作点/常用职位（HR 在员工管理里配置）。';
COMMENT ON COLUMN public.employee_favorite.id IS '自增主键';
COMMENT ON COLUMN public.employee_favorite.emp_id IS '员工工号（employee.emp_id）';
COMMENT ON COLUMN public.employee_favorite.kind IS '类型：region 地区 / furnace 工作点 / post 职位';
COMMENT ON COLUMN public.employee_favorite.value IS '对应取值（地区名/工作点编号/职位名），同一员工同类型可多行';

-- 初始数据
SELECT pg_catalog.setval('public.employee_favorite_id_seq', 1, false);
