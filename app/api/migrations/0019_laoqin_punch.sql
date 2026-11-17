-- ========================================================================
-- 表：laoqin_punch
-- 劳勤（钉钉考勤）打卡记录（运行期导入，对账数据源）。
-- 与 scan_record 按 emp_id+natual_date 对账 → recon_result；格式对应 劳勤打卡记录.csv。
-- ========================================================================

CREATE TABLE public.laoqin_punch (
    id integer NOT NULL,                                    -- 自增主键
    emp_id character varying(32) NOT NULL,                  -- 员工工号
    org_name character varying(128),                        -- 所属组织（一般为工作点编号，如 41号工作点）
    punch_time timestamp with time zone NOT NULL,           -- 打卡时刻（带时区）
    inout_type character varying(16) NOT NULL,              -- 进出类型：进 / 出 / 自动判断
    record_source character varying(64),                    -- 记录来源（如 钉钉打卡）
    natual_date character varying(10) NOT NULL,             -- 归属自然日 YYYY-MM-DD
    raw_name character varying(64)                          -- 原始姓名（导入侧原文，便于人工核对）
);

CREATE SEQUENCE public.laoqin_punch_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.laoqin_punch_id_seq OWNED BY public.laoqin_punch.id;

ALTER TABLE ONLY public.laoqin_punch ALTER COLUMN id SET DEFAULT nextval('public.laoqin_punch_id_seq'::regclass);

ALTER TABLE ONLY public.laoqin_punch
    ADD CONSTRAINT laoqin_punch_pkey PRIMARY KEY (id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.laoqin_punch IS '劳勤（钉钉考勤）打卡记录（运行期导入，对账数据源）。';
COMMENT ON COLUMN public.laoqin_punch.id IS '自增主键';
COMMENT ON COLUMN public.laoqin_punch.emp_id IS '员工工号';
COMMENT ON COLUMN public.laoqin_punch.org_name IS '所属组织（一般为工作点编号，如 41号工作点）';
COMMENT ON COLUMN public.laoqin_punch.punch_time IS '打卡时刻（带时区）';
COMMENT ON COLUMN public.laoqin_punch.inout_type IS '进出类型：进 / 出 / 自动判断';
COMMENT ON COLUMN public.laoqin_punch.record_source IS '记录来源（如 钉钉打卡）';
COMMENT ON COLUMN public.laoqin_punch.natual_date IS '归属自然日 YYYY-MM-DD';
COMMENT ON COLUMN public.laoqin_punch.raw_name IS '原始姓名（导入侧原文，便于人工核对）';

-- 本表不预置数据（运行期产生）
