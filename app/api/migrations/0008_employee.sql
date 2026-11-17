-- ========================================================================
-- 表：employee
-- 员工花名册（用户主数据）：登录身份、角色、与钉钉的绑定关系都在这。
-- 登录链路：钉钉免登拿 userId → 匹配本表 dd_user_id 或 emp_id → 取 role 建会话。
-- 维护入口：管理后台「员工管理」（admin 可增删改；HR 只读）。
-- 初始数据：仅系统管理员账号。员工花名册由管理后台维护，或由演示数据命令创建。
-- ========================================================================

CREATE TABLE public.employee (
    id integer NOT NULL,                                    -- 自增主键（仅内部用；业务一律用 emp_id）
    emp_id character varying(32) NOT NULL,                  -- 工号=业务唯一ID（会话 userid、各业务表 emp_id 都是它）
    name character varying(64) NOT NULL,                    -- 姓名
    id_card character varying(32),                          -- 身份证号（花名册导入，可空）
    dd_user_id character varying(64),                       -- 钉钉 UserId（免登匹配用；初始为空，由 HR 录入）
    role character varying(16),                             -- 角色 → role.code：worker/chief/hr/stat/admin
    sync_status character varying(32) NOT NULL,             -- 数据来源标记：花名册导入/手工/系统
    created_at timestamp with time zone NOT NULL DEFAULT now(),  -- 创建时间（默认=插入时刻）
    updated_at timestamp with time zone NOT NULL DEFAULT now(),  -- 最后修改时间（默认=插入时刻；应用更新时刷新）
    created_by character varying(64),                       -- 创建人（工号或 admin）
    updated_by character varying(64),                       -- 最后修改人（工号或 admin）
    deleted boolean NOT NULL                                -- 软删除标记：true=已删除（仅隐藏不物理删，可追溯）
);

CREATE SEQUENCE public.employee_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.employee_id_seq OWNED BY public.employee.id;

ALTER TABLE ONLY public.employee ALTER COLUMN id SET DEFAULT nextval('public.employee_id_seq'::regclass);

ALTER TABLE ONLY public.employee
    ADD CONSTRAINT employee_emp_id_key UNIQUE (emp_id);

ALTER TABLE ONLY public.employee
    ADD CONSTRAINT employee_pkey PRIMARY KEY (id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.employee IS '员工花名册（用户主数据）：登录身份、角色、与钉钉的绑定关系都在这。';
COMMENT ON COLUMN public.employee.id IS '自增主键（仅内部用；业务一律用 emp_id）';
COMMENT ON COLUMN public.employee.emp_id IS '工号=业务唯一ID（会话 userid、各业务表 emp_id 都是它）';
COMMENT ON COLUMN public.employee.name IS '姓名';
COMMENT ON COLUMN public.employee.id_card IS '身份证号（花名册导入，可空）';
COMMENT ON COLUMN public.employee.dd_user_id IS '钉钉 UserId（免登匹配用；初始为空字符串，由 HR 在员工管理录入）';
COMMENT ON COLUMN public.employee.role IS '角色 → role.code：worker/chief/hr/stat/admin';
COMMENT ON COLUMN public.employee.sync_status IS '数据来源标记：花名册导入/手工/系统';
COMMENT ON COLUMN public.employee.created_at IS '创建时间（UTC；默认=插入时刻）';
COMMENT ON COLUMN public.employee.updated_at IS '最后修改时间（UTC；默认=插入时刻，应用更新时刷新）';
COMMENT ON COLUMN public.employee.created_by IS '创建人（工号或 admin）';
COMMENT ON COLUMN public.employee.updated_by IS '最后修改人（工号或 admin）';
COMMENT ON COLUMN public.employee.deleted IS '软删除标记：true=已删除（仅隐藏不物理删，可追溯）';

-- 初始数据：仅 admin 系统账号。创建/修改时间取列默认 now()；dd_user_id 空串由 HR 后续录入。
INSERT INTO public.employee (id, emp_id, name, id_card, dd_user_id, role, sync_status, created_by, updated_by, deleted) VALUES
	(1, 'admin', '管理员', NULL, '', 'admin', '系统', 'admin', 'admin', false);

SELECT pg_catalog.setval('public.employee_id_seq', 1, true);

