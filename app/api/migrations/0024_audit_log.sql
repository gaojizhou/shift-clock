-- ========================================================================
-- 表：audit_log
-- 操作审计日志（追加写，不改不删）。
-- 记录人为操作：扫码打卡/代签/补录审批/取消打卡/日结确认/异常处置/改额度，以及
-- admin-login/impersonate 等系统操作。管理页「操作日志」展示（HR/统计仅见业务白名单，admin 全量）。
-- ========================================================================

CREATE TABLE public.audit_log (
    id integer NOT NULL,                                    -- 自增主键
    actor character varying(64) NOT NULL,                   -- 操作人（工号或 admin）
    action character varying(64) NOT NULL,                  -- 操作类型编码：scan/makeup_review/confirm_submit/impersonate…
    payload text,                                           -- 操作明细（JSON 或文本）
    ts timestamp with time zone NOT NULL                    -- 操作时刻（带时区）
);

CREATE SEQUENCE public.audit_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.audit_log IS '操作审计日志（追加写，不改不删）。';
COMMENT ON COLUMN public.audit_log.id IS '自增主键';
COMMENT ON COLUMN public.audit_log.actor IS '操作人（工号或 admin）';
COMMENT ON COLUMN public.audit_log.action IS '操作类型编码：scan/makeup_review/confirm_submit/impersonate…';
COMMENT ON COLUMN public.audit_log.payload IS '操作明细（JSON 或文本）';
COMMENT ON COLUMN public.audit_log.ts IS '操作时刻（带时区）';

-- 本表不预置数据（运行期产生）
