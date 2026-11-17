-- ========================================================================
-- 表：shift_window
-- 班次时间窗（按地区）：早/中/晚三班的扫码有效时段（HH:MM 字符串）。
-- 扫码上岗按当前时刻落在哪个窗判定班次归属；来源 班次定义.csv。每地区一行（region 唯一）。
-- ========================================================================

CREATE TABLE public.shift_window (
    id integer NOT NULL,                                    -- 自增主键
    region character varying(64) NOT NULL,                  -- 地区名称（唯一，一地区一行）
    region_code character varying(16),                      -- 地区字典外键 → region.code
    early_from character varying(32) NOT NULL,              -- 早班窗开始 HH:MM
    early_to character varying(32) NOT NULL,                -- 早班窗结束 HH:MM
    mid_from character varying(32) NOT NULL,                -- 中班窗开始 HH:MM
    mid_to character varying(32) NOT NULL,                  -- 中班窗结束 HH:MM
    late_from character varying(32) NOT NULL,               -- 晚班窗开始 HH:MM
    late_to character varying(32) NOT NULL                  -- 晚班窗结束 HH:MM
);

CREATE SEQUENCE public.shift_window_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.shift_window_id_seq OWNED BY public.shift_window.id;

ALTER TABLE ONLY public.shift_window ALTER COLUMN id SET DEFAULT nextval('public.shift_window_id_seq'::regclass);

ALTER TABLE ONLY public.shift_window
    ADD CONSTRAINT shift_window_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.shift_window
    ADD CONSTRAINT shift_window_region_key UNIQUE (region);

ALTER TABLE ONLY public.shift_window
    ADD CONSTRAINT shift_window_region_code_fkey FOREIGN KEY (region_code) REFERENCES public.region(code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.shift_window IS '班次时间窗（按地区）：早/中/晚三班的扫码有效时段（HH:MM 字符串）。';
COMMENT ON COLUMN public.shift_window.id IS '自增主键';
COMMENT ON COLUMN public.shift_window.region IS '地区名称（唯一，一地区一行）';
COMMENT ON COLUMN public.shift_window.region_code IS '地区字典外键 → region.code';
COMMENT ON COLUMN public.shift_window.early_from IS '早班窗开始 HH:MM';
COMMENT ON COLUMN public.shift_window.early_to IS '早班窗结束 HH:MM';
COMMENT ON COLUMN public.shift_window.mid_from IS '中班窗开始 HH:MM';
COMMENT ON COLUMN public.shift_window.mid_to IS '中班窗结束 HH:MM';
COMMENT ON COLUMN public.shift_window.late_from IS '晚班窗开始 HH:MM';
COMMENT ON COLUMN public.shift_window.late_to IS '晚班窗结束 HH:MM';

-- 初始数据
INSERT INTO public.shift_window VALUES
	(1, '示例厂区A', 'REG01', '10:00', '18:00', '18:00', '次日02:00', '02:00', '10:00'),
	(2, '示例厂区B', 'REG02', '09:00', '17:00', '17:00', '次日01:00', '01:00', '09:00');

SELECT pg_catalog.setval('public.shift_window_id_seq', 2, true);
