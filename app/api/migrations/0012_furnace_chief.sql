-- ========================================================================
-- 表：furnace_chief
-- 工作点↔值班长绑定（一工作点多值班长，多对多）。
-- 值班长登录后 /me 返回其绑定工作点，日确认页据此定位；同工作点多个值班长互相可见、均可操作编制。
-- 维护：seed 按花名册值班长岗位绑定；后续人工/SQL 维护（暂无管理界面）。
-- ========================================================================

CREATE TABLE public.furnace_chief (
    id integer NOT NULL,                                    -- 自增主键
    furnace_id integer NOT NULL,                            -- 工作点外键 → furnace.id
    emp_id character varying(32) NOT NULL,                  -- 值班长工号（employee.emp_id）；(furnace_id, emp_id) 唯一
    created_at timestamp with time zone NOT NULL,           -- 创建时间（UTC）
    updated_at timestamp with time zone NOT NULL,           -- 最后修改时间（UTC，更新时自动刷新）
    created_by character varying(64),                       -- 创建人（工号或 admin）
    updated_by character varying(64),                       -- 最后修改人（工号或 admin）
    deleted boolean NOT NULL                                -- 软删除标记：true=已删除（仅隐藏不物理删，可追溯）
);

CREATE SEQUENCE public.furnace_chief_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.furnace_chief_id_seq OWNED BY public.furnace_chief.id;

ALTER TABLE ONLY public.furnace_chief ALTER COLUMN id SET DEFAULT nextval('public.furnace_chief_id_seq'::regclass);

ALTER TABLE ONLY public.furnace_chief
    ADD CONSTRAINT furnace_chief_furnace_id_emp_id_key UNIQUE (furnace_id, emp_id);

ALTER TABLE ONLY public.furnace_chief
    ADD CONSTRAINT furnace_chief_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.furnace_chief
    ADD CONSTRAINT furnace_chief_furnace_id_fkey FOREIGN KEY (furnace_id) REFERENCES public.furnace(id);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.furnace_chief IS '工作点↔值班长绑定（一工作点多值班长，多对多）。';
COMMENT ON COLUMN public.furnace_chief.id IS '自增主键';
COMMENT ON COLUMN public.furnace_chief.furnace_id IS '工作点外键 → furnace.id';
COMMENT ON COLUMN public.furnace_chief.emp_id IS '值班长工号（employee.emp_id）；(furnace_id, emp_id) 唯一';
COMMENT ON COLUMN public.furnace_chief.created_at IS '创建时间（UTC）';
COMMENT ON COLUMN public.furnace_chief.updated_at IS '最后修改时间（UTC，更新时自动刷新）';
COMMENT ON COLUMN public.furnace_chief.created_by IS '创建人（工号或 admin）';
COMMENT ON COLUMN public.furnace_chief.updated_by IS '最后修改人（工号或 admin）';
COMMENT ON COLUMN public.furnace_chief.deleted IS '软删除标记：true=已删除（仅隐藏不物理删，可追溯）';

-- 初始数据：无（演示绑定由 app.seed 载入）。

