-- ========================================================================
-- 表：scan_record
-- 扫码上岗记录（核心交易表，运行期产生）。
-- 操作工扫工作点二维码选岗落一行；值班长代签/补录审批通过也会生成。日确认/对账/导出都读它。
-- ========================================================================

CREATE TABLE public.scan_record (
    id integer NOT NULL,                                    -- 自增主键
    emp_id character varying(32) NOT NULL,                  -- 员工工号（employee.emp_id）
    furnace_id integer NOT NULL,                            -- 工作点外键 → furnace.id
    squad character varying(16),                            -- 班次名称；NULL=不分班岗
    squad_code character varying(16),                       -- 班次字典外键 → squad.code
    post character varying(64) NOT NULL,                    -- 岗位名称（展示冗余）
    position_code character varying(16),                    -- 职位字典外键 → position.code
    employ_type character varying(16) NOT NULL,             -- 用工类型：正式/实习/待岗…（影响编制占用口径）
    scan_time timestamp with time zone NOT NULL,            -- 扫码时刻（带时区）
    source character varying(16) NOT NULL,                  -- 来源：扫码 / 代签 / 补录
    status character varying(16) NOT NULL,                  -- 状态：active 有效 / void 已撤回（值班长日确认可取消打卡）
    natual_date character varying(10) NOT NULL,             -- 归属自然日 YYYY-MM-DD（按班次窗归一，跨午夜班次归前一日）
    day_fraction double precision DEFAULT '1'::double precision NOT NULL-- 出勤时长占比：整天=1.0，半天=0.5（值班长日结侧标注，算薪口径）
);

CREATE SEQUENCE public.scan_record_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.scan_record_id_seq OWNED BY public.scan_record.id;

ALTER TABLE ONLY public.scan_record ALTER COLUMN id SET DEFAULT nextval('public.scan_record_id_seq'::regclass);

ALTER TABLE ONLY public.scan_record
    ADD CONSTRAINT scan_record_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.scan_record
    ADD CONSTRAINT scan_record_furnace_id_fkey FOREIGN KEY (furnace_id) REFERENCES public.furnace(id);

ALTER TABLE ONLY public.scan_record
    ADD CONSTRAINT scan_record_position_code_fkey FOREIGN KEY (position_code) REFERENCES public."position"(code);

ALTER TABLE ONLY public.scan_record
    ADD CONSTRAINT scan_record_squad_code_fkey FOREIGN KEY (squad_code) REFERENCES public.squad(code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.scan_record IS '扫码上岗记录（核心交易表，运行期产生）。';
COMMENT ON COLUMN public.scan_record.id IS '自增主键';
COMMENT ON COLUMN public.scan_record.emp_id IS '员工工号（employee.emp_id）';
COMMENT ON COLUMN public.scan_record.furnace_id IS '工作点外键 → furnace.id';
COMMENT ON COLUMN public.scan_record.squad IS '班次名称；NULL=不分班岗';
COMMENT ON COLUMN public.scan_record.squad_code IS '班次字典外键 → squad.code';
COMMENT ON COLUMN public.scan_record.post IS '岗位名称（展示冗余）';
COMMENT ON COLUMN public.scan_record.position_code IS '职位字典外键 → position.code';
COMMENT ON COLUMN public.scan_record.employ_type IS '用工类型：正式/实习/待岗…（影响编制占用口径）';
COMMENT ON COLUMN public.scan_record.scan_time IS '扫码时刻（带时区）';
COMMENT ON COLUMN public.scan_record.source IS '来源：扫码 / 代签 / 补录';
COMMENT ON COLUMN public.scan_record.status IS '状态：active 有效 / void 已撤回（值班长日确认可取消打卡）';
COMMENT ON COLUMN public.scan_record.natual_date IS '归属自然日 YYYY-MM-DD（按班次窗归一，跨午夜班次归前一日）';
COMMENT ON COLUMN public.scan_record.day_fraction IS '出勤时长占比：整天=1.0，半天=0.5（值班长日结侧标注，算薪口径）';

-- 本表不预置数据（运行期产生）
