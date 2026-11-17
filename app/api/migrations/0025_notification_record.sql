-- ========================================================================
-- 表：notification_record
-- 通知发送记录（钉钉工作通知等出站消息的留痕）。
-- ========================================================================

CREATE TABLE public.notification_record (
    id integer NOT NULL,                                    -- 自增主键
    channel character varying(32) NOT NULL,                 -- 渠道：dingtalk_work / mock…
    target character varying(256) NOT NULL,                 -- 接收目标（钉钉 userId / 工号）
    payload text,                                           -- 消息内容（JSON 文本）
    ts timestamp with time zone NOT NULL                    -- 发送时刻（带时区）
);

CREATE SEQUENCE public.notification_record_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.notification_record_id_seq OWNED BY public.notification_record.id;

ALTER TABLE ONLY public.notification_record ALTER COLUMN id SET DEFAULT nextval('public.notification_record_id_seq'::regclass);

ALTER TABLE ONLY public.notification_record
    ADD CONSTRAINT notification_record_pkey PRIMARY KEY (id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.notification_record IS '通知发送记录（钉钉工作通知等出站消息的留痕）。';
COMMENT ON COLUMN public.notification_record.id IS '自增主键';
COMMENT ON COLUMN public.notification_record.channel IS '渠道：dingtalk_work / mock…';
COMMENT ON COLUMN public.notification_record.target IS '接收目标（钉钉 userId / 工号）';
COMMENT ON COLUMN public.notification_record.payload IS '消息内容（JSON 文本）';
COMMENT ON COLUMN public.notification_record.ts IS '发送时刻（带时区）';

-- 本表不预置数据（运行期产生）
