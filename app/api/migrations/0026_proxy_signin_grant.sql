-- ========================================================================
-- 表：proxy_signin_grant
-- 代签额度加量记录：人事专员/统计员给某员工某月临时增加代签次数。
-- 有效额度 = app_setting 默认上限 + 当月 grant 累计 extra。管理页「代签额度」维护。
-- ========================================================================

CREATE TABLE public.proxy_signin_grant (
    id integer NOT NULL,                                    -- 自增主键
    emp_id character varying(32) NOT NULL,                  -- 员工工号
    month character varying(7) NOT NULL,                    -- 生效月份 YYYY-MM
    extra integer NOT NULL,                                 -- 增加的次数（可叠加多条）
    reason text,                                            -- 加量原因
    actor character varying(64) NOT NULL,                   -- 操作人（HR/统计工号）
    ts timestamp with time zone NOT NULL                    -- 操作时刻（带时区）
);

CREATE SEQUENCE public.proxy_signin_grant_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.proxy_signin_grant_id_seq OWNED BY public.proxy_signin_grant.id;

ALTER TABLE ONLY public.proxy_signin_grant ALTER COLUMN id SET DEFAULT nextval('public.proxy_signin_grant_id_seq'::regclass);

ALTER TABLE ONLY public.proxy_signin_grant
    ADD CONSTRAINT proxy_signin_grant_pkey PRIMARY KEY (id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.proxy_signin_grant IS '代签额度加量记录：人事专员/统计员给某员工某月临时增加代签次数。';
COMMENT ON COLUMN public.proxy_signin_grant.id IS '自增主键';
COMMENT ON COLUMN public.proxy_signin_grant.emp_id IS '员工工号';
COMMENT ON COLUMN public.proxy_signin_grant.month IS '生效月份 YYYY-MM';
COMMENT ON COLUMN public.proxy_signin_grant.extra IS '增加的次数（可叠加多条）';
COMMENT ON COLUMN public.proxy_signin_grant.reason IS '加量原因';
COMMENT ON COLUMN public.proxy_signin_grant.actor IS '操作人（HR/统计工号）';
COMMENT ON COLUMN public.proxy_signin_grant.ts IS '操作时刻（带时区）';

-- 本表不预置数据（运行期产生）
