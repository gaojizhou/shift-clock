-- ========================================================================
-- 表：squad_schedule
-- 班次时间表：每地区每班的起止时间（HH:MM）。end<=start 表示跨午夜
-- （如 18:00-次日02:00）。与 shift_window（扫码有效窗）相辅：本表描述班次本身的作息区间。
-- ========================================================================

CREATE TABLE public.squad_schedule (
    id integer NOT NULL,                                    -- 自增主键
    region character varying(64) NOT NULL,                  -- 地区名称（(region,squad) 唯一）
    region_code character varying(16),                      -- 地区字典外键 → region.code
    squad character varying(16) NOT NULL,                   -- 班次名称：1班/2班/3班
    squad_code character varying(16),                       -- 班次字典外键 → squad.code
    start_time character varying(5) NOT NULL,               -- 班次开始 HH:MM（如 02:00）
    end_time character varying(5) NOT NULL,                 -- 班次结束 HH:MM；<=start_time 即跨午夜
    created_at timestamp with time zone NOT NULL,           -- 创建时间（UTC）
    updated_at timestamp with time zone NOT NULL,           -- 最后修改时间（UTC，更新时自动刷新）
    created_by character varying(64),                       -- 创建人（工号或 admin）
    updated_by character varying(64),                       -- 最后修改人（工号或 admin）
    deleted boolean NOT NULL                                -- 软删除标记：true=已删除（仅隐藏不物理删，可追溯）
);

CREATE SEQUENCE public.squad_schedule_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.squad_schedule_id_seq OWNED BY public.squad_schedule.id;

ALTER TABLE ONLY public.squad_schedule ALTER COLUMN id SET DEFAULT nextval('public.squad_schedule_id_seq'::regclass);

ALTER TABLE ONLY public.squad_schedule
    ADD CONSTRAINT squad_schedule_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.squad_schedule
    ADD CONSTRAINT squad_schedule_region_squad_key UNIQUE (region, squad);

ALTER TABLE ONLY public.squad_schedule
    ADD CONSTRAINT squad_schedule_region_code_fkey FOREIGN KEY (region_code) REFERENCES public.region(code);

ALTER TABLE ONLY public.squad_schedule
    ADD CONSTRAINT squad_schedule_squad_code_fkey FOREIGN KEY (squad_code) REFERENCES public.squad(code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.squad_schedule IS '班次时间表：每地区每班的起止时间（HH:MM）。end<=start 表示跨午夜';
COMMENT ON COLUMN public.squad_schedule.id IS '自增主键';
COMMENT ON COLUMN public.squad_schedule.region IS '地区名称（(region,squad) 唯一）';
COMMENT ON COLUMN public.squad_schedule.region_code IS '地区字典外键 → region.code';
COMMENT ON COLUMN public.squad_schedule.squad IS '班次名称：1班/2班/3班';
COMMENT ON COLUMN public.squad_schedule.squad_code IS '班次字典外键 → squad.code';
COMMENT ON COLUMN public.squad_schedule.start_time IS '班次开始 HH:MM（如 02:00）';
COMMENT ON COLUMN public.squad_schedule.end_time IS '班次结束 HH:MM；<=start_time 即跨午夜';
COMMENT ON COLUMN public.squad_schedule.created_at IS '创建时间（UTC）';
COMMENT ON COLUMN public.squad_schedule.updated_at IS '最后修改时间（UTC，更新时自动刷新）';
COMMENT ON COLUMN public.squad_schedule.created_by IS '创建人（工号或 admin）';
COMMENT ON COLUMN public.squad_schedule.updated_by IS '最后修改人（工号或 admin）';
COMMENT ON COLUMN public.squad_schedule.deleted IS '软删除标记：true=已删除（仅隐藏不物理删，可追溯）';

-- 初始数据
SELECT pg_catalog.setval('public.squad_schedule_id_seq', 1, false);
