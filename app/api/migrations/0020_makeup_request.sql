-- ========================================================================
-- 表：makeup_request
-- 补录/代签申请（运行期产生）。
-- kind=补录：工人漏扫后自行发起，待审→值班长/HR 审批；kind=代签：值班长日确认页直接代签
-- （每人每月有上限，见 app_setting + proxy_signin_grant）。通过后生成 scan_record。
-- ========================================================================

CREATE TABLE public.makeup_request (
    id integer NOT NULL,                                    -- 自增主键
    emp_id character varying(32) NOT NULL,                  -- 员工工号
    furnace_id integer NOT NULL,                            -- 工作点外键 → furnace.id
    squad character varying(16),                            -- 班次名称；NULL=不分班岗
    squad_code character varying(16),                       -- 班次字典外键 → squad.code
    post character varying(64) NOT NULL,                    -- 岗位名称
    position_code character varying(16),                    -- 职位字典外键 → position.code
    employ_type character varying(16) NOT NULL,             -- 用工类型（默认 正式）
    natual_date character varying(10) NOT NULL,             -- 补录归属自然日 YYYY-MM-DD
    reason text,                                            -- 申请原因（自由文本）
    status character varying(16) NOT NULL,                  -- 状态：待审 / 通过 / 拒绝
    approver character varying(64),                         -- 审批人工号
    kind character varying(16) NOT NULL,                    -- 类型：补录（工人发起）/ 代签（值班长发起，按月限额）
    day_fraction double precision DEFAULT '1'::double precision NOT NULL-- 出勤时长占比：整班=1.0，半班=0.5
);

CREATE SEQUENCE public.makeup_request_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.makeup_request_id_seq OWNED BY public.makeup_request.id;

ALTER TABLE ONLY public.makeup_request ALTER COLUMN id SET DEFAULT nextval('public.makeup_request_id_seq'::regclass);

ALTER TABLE ONLY public.makeup_request
    ADD CONSTRAINT makeup_request_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.makeup_request
    ADD CONSTRAINT makeup_request_furnace_id_fkey FOREIGN KEY (furnace_id) REFERENCES public.furnace(id);

ALTER TABLE ONLY public.makeup_request
    ADD CONSTRAINT makeup_request_position_code_fkey FOREIGN KEY (position_code) REFERENCES public."position"(code);

ALTER TABLE ONLY public.makeup_request
    ADD CONSTRAINT makeup_request_squad_code_fkey FOREIGN KEY (squad_code) REFERENCES public.squad(code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.makeup_request IS '补录/代签申请（运行期产生）。';
COMMENT ON COLUMN public.makeup_request.id IS '自增主键';
COMMENT ON COLUMN public.makeup_request.emp_id IS '员工工号';
COMMENT ON COLUMN public.makeup_request.furnace_id IS '工作点外键 → furnace.id';
COMMENT ON COLUMN public.makeup_request.squad IS '班次名称；NULL=不分班岗';
COMMENT ON COLUMN public.makeup_request.squad_code IS '班次字典外键 → squad.code';
COMMENT ON COLUMN public.makeup_request.post IS '岗位名称';
COMMENT ON COLUMN public.makeup_request.position_code IS '职位字典外键 → position.code';
COMMENT ON COLUMN public.makeup_request.employ_type IS '用工类型（默认 正式）';
COMMENT ON COLUMN public.makeup_request.natual_date IS '补录归属自然日 YYYY-MM-DD';
COMMENT ON COLUMN public.makeup_request.reason IS '申请原因（自由文本）';
COMMENT ON COLUMN public.makeup_request.status IS '状态：待审 / 通过 / 拒绝';
COMMENT ON COLUMN public.makeup_request.approver IS '审批人工号';
COMMENT ON COLUMN public.makeup_request.kind IS '类型：补录（工人发起）/ 代签（值班长发起，按月限额）';
COMMENT ON COLUMN public.makeup_request.day_fraction IS '出勤时长占比：整班=1.0，半班=0.5';

-- 本表不预置数据（运行期产生）
