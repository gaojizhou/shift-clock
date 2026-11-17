-- ========================================================================
-- 钉钉通讯录同步：原始部门树 + 原始员工 + 同步进度状态
-- 维护入口：管理后台「通讯录同步」（同步按钮[后台跑]→进度轮询→转为员工花名册）。
-- 同步范围由 roster_root_dept 表控制（HR 在页面维护）。
-- ========================================================================

-- 1) 原始部门树（BFS listsub 同步而来，缓存供成员同步复用）
CREATE TABLE public.dingtalk_department_raw (
    id integer NOT NULL,
    dept_id bigint NOT NULL,                                 -- 钉钉部门ID
    parent_id bigint,                                        -- 父部门ID
    name character varying(128),                             -- 部门名
    layer character varying(1000),                           -- 完整层级路径
    face_count integer,                                      -- ext.faceCount（参考，非编制数）
    root_dept_id bigint,                                     -- 归属哪个配置根部门
    synced_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.dingtalk_department_raw_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.dingtalk_department_raw_id_seq OWNED BY public.dingtalk_department_raw.id;
ALTER TABLE ONLY public.dingtalk_department_raw ALTER COLUMN id SET DEFAULT nextval('public.dingtalk_department_raw_id_seq'::regclass);
ALTER TABLE ONLY public.dingtalk_department_raw ADD CONSTRAINT dingtalk_department_raw_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.dingtalk_department_raw ADD CONSTRAINT uq_dingtalk_dept_raw_deptid UNIQUE (dept_id);
CREATE INDEX ix_dingtalk_dept_raw_root ON public.dingtalk_department_raw (root_dept_id);
COMMENT ON TABLE public.dingtalk_department_raw IS '钉钉通讯录原始部门树（listsub 同步，成员同步复用）。';

-- 2) 原始员工（user/list 同步而来，按 userid 去重）
CREATE TABLE public.dingtalk_employee_raw (
    id integer NOT NULL,
    userid character varying(64) NOT NULL,                  -- 钉钉 userid（业务唯一键）
    name character varying(64),                             -- 姓名
    job_number character varying(64),                       -- 工号
    title character varying(128),                           -- 职位
    mobile character varying(32),                           -- 手机号（需字段权限，可空）
    unionid character varying(128),                         -- 跨组织唯一ID
    hired_date bigint,                                      -- 入职时间(ms)
    active boolean,                                         -- 是否激活
    dept_id bigint,                                         -- 发现所在末级部门
    dept_path character varying(1000),                      -- 末级部门完整路径
    root_label character varying(64),                       -- 归属根部门标签（示例厂区A/示例厂区B生产部）
    synced_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.dingtalk_employee_raw_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.dingtalk_employee_raw_id_seq OWNED BY public.dingtalk_employee_raw.id;
ALTER TABLE ONLY public.dingtalk_employee_raw ALTER COLUMN id SET DEFAULT nextval('public.dingtalk_employee_raw_id_seq'::regclass);
ALTER TABLE ONLY public.dingtalk_employee_raw ADD CONSTRAINT dingtalk_employee_raw_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.dingtalk_employee_raw ADD CONSTRAINT uq_dingtalk_emp_raw_userid UNIQUE (userid);
CREATE INDEX ix_dingtalk_emp_raw_jobnumber ON public.dingtalk_employee_raw (job_number);
COMMENT ON TABLE public.dingtalk_employee_raw IS '钉钉通讯录原始员工（user/list 同步，按 userid 去重）。';

-- 3) 同步进度状态（后台任务进度，前端轮询）
CREATE TABLE public.dingtalk_sync_state (
    id integer NOT NULL,
    kind character varying(32) NOT NULL,                    -- 同步种类，如 'roster'
    status character varying(16) DEFAULT 'idle' NOT NULL,   -- idle/running/done/error
    phase character varying(32),                            -- 校验根/部门树/人员/完成
    current_root character varying(64),                     -- 当前同步的根部门标签
    dept_total integer DEFAULT 0 NOT NULL,
    dept_done integer DEFAULT 0 NOT NULL,
    member_total_depts integer DEFAULT 0 NOT NULL,
    member_done_depts integer DEFAULT 0 NOT NULL,
    member_count integer DEFAULT 0 NOT NULL,
    message text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.dingtalk_sync_state_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.dingtalk_sync_state_id_seq OWNED BY public.dingtalk_sync_state.id;
ALTER TABLE ONLY public.dingtalk_sync_state ALTER COLUMN id SET DEFAULT nextval('public.dingtalk_sync_state_id_seq'::regclass);
ALTER TABLE ONLY public.dingtalk_sync_state ADD CONSTRAINT dingtalk_sync_state_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.dingtalk_sync_state ADD CONSTRAINT uq_dingtalk_sync_state_kind UNIQUE (kind);
COMMENT ON TABLE public.dingtalk_sync_state IS '钉钉同步进度状态（后台任务进度，前端轮询）。';

-- 初始一行 roster 状态
INSERT INTO public.dingtalk_sync_state (kind, status) VALUES ('roster', 'idle');
