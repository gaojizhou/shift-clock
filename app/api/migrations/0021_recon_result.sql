-- ========================================================================
-- 表：recon_result
-- 劳勤×扫码对账结果（按 员工+日期 一行，重跑覆盖）。
-- 状态：一致 / 劳勤有工作点无（漏扫）/ 工作点有劳勤无（缺劳勤）…；异常工作台据此生成待办。
-- ========================================================================

CREATE TABLE public.recon_result (
    id integer NOT NULL,                                    -- 自增主键
    emp_id character varying(32) NOT NULL,                  -- 员工工号（(emp_id,date) 唯一）
    date character varying(10) NOT NULL,                    -- 对账日期 YYYY-MM-DD
    status character varying(32) NOT NULL,                  -- 对账状态：一致/劳勤有工作点无/工作点有劳勤无…
    detail text                                             -- 明细（JSON 文本：时间差、证据等）
);

CREATE SEQUENCE public.recon_result_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.recon_result_id_seq OWNED BY public.recon_result.id;

ALTER TABLE ONLY public.recon_result ALTER COLUMN id SET DEFAULT nextval('public.recon_result_id_seq'::regclass);

ALTER TABLE ONLY public.recon_result
    ADD CONSTRAINT recon_result_emp_id_date_key UNIQUE (emp_id, date);

ALTER TABLE ONLY public.recon_result
    ADD CONSTRAINT recon_result_pkey PRIMARY KEY (id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.recon_result IS '劳勤×扫码对账结果（按 员工+日期 一行，重跑覆盖）。';
COMMENT ON COLUMN public.recon_result.id IS '自增主键';
COMMENT ON COLUMN public.recon_result.emp_id IS '员工工号（(emp_id,date) 唯一）';
COMMENT ON COLUMN public.recon_result.date IS '对账日期 YYYY-MM-DD';
COMMENT ON COLUMN public.recon_result.status IS '对账状态：一致/劳勤有工作点无/工作点有劳勤无…';
COMMENT ON COLUMN public.recon_result.detail IS '明细（JSON 文本：时间差、证据等）';

-- 本表不预置数据（运行期产生）
