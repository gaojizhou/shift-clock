-- ========================================================================
-- 表：quota_rule
-- 工作点编制（每工作点每班每岗的在岗人数）——本文件只建表不填数据。
-- 数据来源：管理后台「设备类型编制模板（批量）」按设备类型批量应用（覆盖式展开：squadless 岗
-- 1 行 squad=NULL；分班岗展开 1/2/3 班各 1 行），或「编辑当前工作点编制」单工作点覆盖。
-- quota 视图（0027）基于本表，薪资侧按 地区/工作点/班次/岗位 查编制。
-- ========================================================================

CREATE TABLE public.quota_rule (
    id integer NOT NULL,                                    -- 自增主键
    furnace_id integer NOT NULL,                            -- 工作点外键 → furnace.id
    squad character varying(16),                            -- 班次名称：1班/2班/3班；NULL=不分班岗位（值班长/维修工）
    squad_code character varying(16),                       -- 班次字典外键 → squad.code（SQx），squad=NULL 时为 NULL
    post character varying(64) NOT NULL,                    -- 岗位名称（展示冗余；规范引用是 position_code）
    position_code character varying(16),                    -- 职位字典外键 → position.code（POSxx）
    capacity integer NOT NULL                               -- 该工作点该班该岗编制人数（正式岗位坑位数）
);

CREATE SEQUENCE public.quota_rule_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.quota_rule_id_seq OWNED BY public.quota_rule.id;

ALTER TABLE ONLY public.quota_rule ALTER COLUMN id SET DEFAULT nextval('public.quota_rule_id_seq'::regclass);

ALTER TABLE ONLY public.quota_rule
    ADD CONSTRAINT quota_rule_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.quota_rule
    ADD CONSTRAINT quota_rule_furnace_id_fkey FOREIGN KEY (furnace_id) REFERENCES public.furnace(id);

ALTER TABLE ONLY public.quota_rule
    ADD CONSTRAINT quota_rule_position_code_fkey FOREIGN KEY (position_code) REFERENCES public."position"(code);

ALTER TABLE ONLY public.quota_rule
    ADD CONSTRAINT quota_rule_squad_code_fkey FOREIGN KEY (squad_code) REFERENCES public.squad(code);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.quota_rule IS '工作点编制（每工作点每班每岗的在岗人数）——本文件只建表不填数据。';
COMMENT ON COLUMN public.quota_rule.id IS '自增主键';
COMMENT ON COLUMN public.quota_rule.furnace_id IS '工作点外键 → furnace.id';
COMMENT ON COLUMN public.quota_rule.squad IS '班次名称：1班/2班/3班；NULL=不分班岗位（值班长/维修工）';
COMMENT ON COLUMN public.quota_rule.squad_code IS '班次字典外键 → squad.code（SQx），squad=NULL 时为 NULL';
COMMENT ON COLUMN public.quota_rule.post IS '岗位名称（展示冗余；规范引用是 position_code）';
COMMENT ON COLUMN public.quota_rule.position_code IS '职位字典外键 → position.code（POSxx）';
COMMENT ON COLUMN public.quota_rule.capacity IS '该工作点该班该岗编制人数（正式岗位坑位数）';

-- 本表不预置数据（运行期产生）
