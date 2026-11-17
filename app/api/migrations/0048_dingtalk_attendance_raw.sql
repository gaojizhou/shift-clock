-- ========================================================================
-- 表：dingtalk_attendance_raw
-- 钉钉考勤打卡「原始流水」：从外部 MySQL 视图 `<database>.<view>`
-- 按日期范围同步而来，原样镜像源字段。业务键(code, att_time, direct, src)幂等去重。
-- 再经「转为劳勤考勤」映射成 laoqin_punch（劳勤工作台对账数据源）。
-- 维护入口：管理后台「原始考勤同步」（同步按钮 + 转换按钮）。
-- ========================================================================

CREATE TABLE public.dingtalk_attendance_raw (
    id integer NOT NULL,                                    -- 自增主键
    tenant_code character varying(50),                      -- 租户代码（源 tenant_code）
    code character varying(50) NOT NULL,                    -- 工号（=钉钉 userid，源 code）
    name character varying(64),                             -- 姓名（源 name）
    org character varying(64),                              -- 末级组织/工作点编号（源 org）
    layer character varying(1000),                          -- 完整层级路径（源 layer）
    att_time timestamp with time zone NOT NULL,             -- 打卡时刻（源 att_time）
    direct character varying(8) DEFAULT '' NOT NULL,        -- 方向：进/出/自动判断（源 direct）
    state character varying(8),                             -- 状态（源 state，如 有效）
    src character varying(16) DEFAULT '' NOT NULL,          -- 来源（源 src，如 钉钉打卡）
    detail text,                                            -- 明细 JSON：打卡机地址等（源 detail）
    natual_date character varying(10) NOT NULL,             -- 归属自然日 YYYY-MM-DD = date(att_time)
    src_create_time timestamp with time zone,               -- 源 create_time
    src_modify_time timestamp with time zone,               -- 源 modify_time
    synced_at timestamp with time zone DEFAULT now() NOT NULL  -- 本系统同步入库时刻
);

CREATE SEQUENCE public.dingtalk_attendance_raw_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.dingtalk_attendance_raw_id_seq OWNED BY public.dingtalk_attendance_raw.id;

ALTER TABLE ONLY public.dingtalk_attendance_raw
    ALTER COLUMN id SET DEFAULT nextval('public.dingtalk_attendance_raw_id_seq'::regclass);

ALTER TABLE ONLY public.dingtalk_attendance_raw
    ADD CONSTRAINT dingtalk_attendance_raw_pkey PRIMARY KEY (id);

-- 业务键唯一：同人同时刻同方向同来源只存一条（同步幂等 upsert 依赖此约束）
ALTER TABLE ONLY public.dingtalk_attendance_raw
    ADD CONSTRAINT uq_dingtalk_att_raw_bizkey UNIQUE (code, att_time, direct, src);

-- 同步/查询按 att_time、natual_date 过滤
CREATE INDEX ix_dingtalk_att_raw_att_time ON public.dingtalk_attendance_raw (att_time);
CREATE INDEX ix_dingtalk_att_raw_natual_date ON public.dingtalk_attendance_raw (natual_date);
CREATE INDEX ix_dingtalk_att_raw_code ON public.dingtalk_attendance_raw (code);

-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.dingtalk_attendance_raw IS '钉钉考勤打卡原始流水（从外部视图 view_external_att_data 同步）。';
COMMENT ON COLUMN public.dingtalk_attendance_raw.code IS '工号（=钉钉 userid，源 code）';
COMMENT ON COLUMN public.dingtalk_attendance_raw.att_time IS '打卡时刻（源 att_time）';
COMMENT ON COLUMN public.dingtalk_attendance_raw.direct IS '方向：进/出/自动判断（源 direct）';
COMMENT ON COLUMN public.dingtalk_attendance_raw.src IS '来源（源 src，如 钉钉打卡）';
COMMENT ON COLUMN public.dingtalk_attendance_raw.natual_date IS '归属自然日 YYYY-MM-DD = date(att_time)';
COMMENT ON COLUMN public.dingtalk_attendance_raw.synced_at IS '本系统同步入库时刻';

-- 本表不预置数据（运行期由「原始考勤同步」产生）
