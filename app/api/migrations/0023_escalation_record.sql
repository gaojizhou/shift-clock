-- ========================================================================
-- 表：escalation_record
-- 超时未确认升级提醒记录（每个工作点每日每级一行）。
-- 值班长未按时日确认 → 按级别（1=值班长本人，2=上级…）推送钉钉工作通知并落一行防重发。
-- ========================================================================

CREATE TABLE public.escalation_record (
    id integer NOT NULL,                                    -- 自增主键
    furnace_id integer NOT NULL,                            -- 工作点外键 → furnace.id
    natual_date character varying(10) NOT NULL,             -- 未确认的自然日 YYYY-MM-DD
    level integer NOT NULL,                                 -- 升级级别：1 提醒值班长，2 升级上级…（同工作点同日同级唯一）
    targets character varying(256) NOT NULL,                -- 推送目标（工号/钉钉id 列表，逗号分隔）
    status character varying(32) NOT NULL,                  -- 状态：open 进行中 / resolved 已确认关闭
    first_escalated_at timestamp with time zone NOT NULL    -- 首次升级推送时刻（带时区）
);

CREATE SEQUENCE public.escalation_record_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.escalation_record_id_seq OWNED BY public.escalation_record.id;

ALTER TABLE ONLY public.escalation_record ALTER COLUMN id SET DEFAULT nextval('public.escalation_record_id_seq'::regclass);

ALTER TABLE ONLY public.escalation_record
    ADD CONSTRAINT escalation_record_furnace_id_natual_date_level_key UNIQUE (furnace_id, natual_date, level);

ALTER TABLE ONLY public.escalation_record
    ADD CONSTRAINT escalation_record_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.escalation_record
    ADD CONSTRAINT escalation_record_furnace_id_fkey FOREIGN KEY (furnace_id) REFERENCES public.furnace(id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.escalation_record IS '超时未确认升级提醒记录（每个工作点每日每级一行）。';
COMMENT ON COLUMN public.escalation_record.id IS '自增主键';
COMMENT ON COLUMN public.escalation_record.furnace_id IS '工作点外键 → furnace.id';
COMMENT ON COLUMN public.escalation_record.natual_date IS '未确认的自然日 YYYY-MM-DD';
COMMENT ON COLUMN public.escalation_record.level IS '升级级别：1 提醒值班长，2 升级上级…（同工作点同日同级唯一）';
COMMENT ON COLUMN public.escalation_record.targets IS '推送目标（工号/钉钉id 列表，逗号分隔）';
COMMENT ON COLUMN public.escalation_record.status IS '状态：open 进行中 / resolved 已确认关闭';
COMMENT ON COLUMN public.escalation_record.first_escalated_at IS '首次升级推送时刻（带时区）';

-- 本表不预置数据（运行期产生）
