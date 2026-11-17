-- ========================================================================
-- 表：chief_confirm
-- 值班长日确认（每个工作点每日一行，(furnace_id,natual_date) 唯一）。
-- 值班长在 H5「全部确认提交」后锁定当日（locked=true），编制变动/代签等不再允许。
-- ========================================================================

CREATE TABLE public.chief_confirm (
    id integer NOT NULL,                                    -- 自增主键
    furnace_id integer NOT NULL,                            -- 工作点外键 → furnace.id
    natual_date character varying(10) NOT NULL,             -- 确认归属自然日 YYYY-MM-DD
    locked boolean NOT NULL,                                -- true=已确认锁定（当日不可再改）
    confirmed_by character varying(64),                     -- 确认人（值班长工号）
    confirmed_at timestamp with time zone,                  -- 确认时刻（带时区）
    detail text                                             -- 确认时的快照明细（JSON 文本）
);

CREATE SEQUENCE public.chief_confirm_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.chief_confirm_id_seq OWNED BY public.chief_confirm.id;

ALTER TABLE ONLY public.chief_confirm ALTER COLUMN id SET DEFAULT nextval('public.chief_confirm_id_seq'::regclass);

ALTER TABLE ONLY public.chief_confirm
    ADD CONSTRAINT chief_confirm_furnace_id_natual_date_key UNIQUE (furnace_id, natual_date);

ALTER TABLE ONLY public.chief_confirm
    ADD CONSTRAINT chief_confirm_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.chief_confirm
    ADD CONSTRAINT chief_confirm_furnace_id_fkey FOREIGN KEY (furnace_id) REFERENCES public.furnace(id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.chief_confirm IS '值班长日确认（每个工作点每日一行，(furnace_id,natual_date) 唯一）。';
COMMENT ON COLUMN public.chief_confirm.id IS '自增主键';
COMMENT ON COLUMN public.chief_confirm.furnace_id IS '工作点外键 → furnace.id';
COMMENT ON COLUMN public.chief_confirm.natual_date IS '确认归属自然日 YYYY-MM-DD';
COMMENT ON COLUMN public.chief_confirm.locked IS 'true=已确认锁定（当日不可再改）';
COMMENT ON COLUMN public.chief_confirm.confirmed_by IS '确认人（值班长工号）';
COMMENT ON COLUMN public.chief_confirm.confirmed_at IS '确认时刻（带时区）';
COMMENT ON COLUMN public.chief_confirm.detail IS '确认时的快照明细（JSON 文本）';

-- 本表不预置数据（运行期产生）
