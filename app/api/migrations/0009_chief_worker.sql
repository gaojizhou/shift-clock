-- ========================================================================
-- 表：chief_worker
-- 值班长-常用工人映射（代签名单）：值班长日确认页代签时优先展示这些工人。
-- 值班长本人或 HR/admin 维护（H5「管理常用员工」、员工管理抽屉「值班长关联工人」）。
-- 纯工号字符串关联，无外键约束。初始为空，运行期维护。
-- ========================================================================

CREATE TABLE public.chief_worker (
    id integer NOT NULL,                                    -- 自增主键
    chief_user_id character varying(64) NOT NULL,           -- 值班长工号（employee.emp_id）
    emp_id character varying(64) NOT NULL                   -- 被关联工人工号（employee.emp_id）
);

CREATE SEQUENCE public.chief_worker_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.chief_worker_id_seq OWNED BY public.chief_worker.id;

ALTER TABLE ONLY public.chief_worker ALTER COLUMN id SET DEFAULT nextval('public.chief_worker_id_seq'::regclass);

ALTER TABLE ONLY public.chief_worker
    ADD CONSTRAINT chief_worker_chief_user_id_emp_id_key UNIQUE (chief_user_id, emp_id);

ALTER TABLE ONLY public.chief_worker
    ADD CONSTRAINT chief_worker_pkey PRIMARY KEY (id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.chief_worker IS '值班长-常用工人映射（代签名单）：值班长日确认页代签时优先展示这些工人。';
COMMENT ON COLUMN public.chief_worker.id IS '自增主键';
COMMENT ON COLUMN public.chief_worker.chief_user_id IS '值班长工号（employee.emp_id）';
COMMENT ON COLUMN public.chief_worker.emp_id IS '被关联工人工号（employee.emp_id）';

-- 初始数据
SELECT pg_catalog.setval('public.chief_worker_id_seq', 1, false);
