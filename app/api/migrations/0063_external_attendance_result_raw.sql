-- ========================================================================
-- 表：external_attendance_result_raw
-- 外部考勤结果（日结果视图 view_external_att_result）原始镜像。
-- 按 user_code + kq_date 幂等覆盖，维护入口为管理后台「原始考勤同步 / 考勤结果」。
-- ========================================================================

CREATE TABLE public.external_attendance_result_raw (
    id integer NOT NULL,
    tenant_code character varying(50),
    dept_name character varying(128),
    layer_name character varying(1000),
    hr_status character varying(32),
    hr_type character varying(32),
    user_code character varying(50) NOT NULL,
    user_name character varying(64),
    kq_date_show character varying(16),
    kq_date_title character varying(32),
    kq_date character varying(10) NOT NULL,
    kqqs character varying(16),
    status_name character varying(64),
    lx_type character varying(32),
    actual_on character varying(32),
    actual_off character varying(32),
    quota character varying(32),
    cqgs character varying(32),
    kggs character varying(32),
    cd2 character varying(32),
    zt2 character varying(32),
    ot1 character varying(32),
    ot2 character varying(32),
    ot3 character varying(32),
    leave_01 character varying(32),
    leave_02 character varying(32),
    leave_03 character varying(32),
    ext1 character varying(32),
    ext2 character varying(32),
    business_01 character varying(32),
    business_02 character varying(32),
    synced_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.external_attendance_result_raw_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.external_attendance_result_raw_id_seq OWNED BY public.external_attendance_result_raw.id;

ALTER TABLE ONLY public.external_attendance_result_raw
    ALTER COLUMN id SET DEFAULT nextval('public.external_attendance_result_raw_id_seq'::regclass);

ALTER TABLE ONLY public.external_attendance_result_raw
    ADD CONSTRAINT external_attendance_result_raw_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.external_attendance_result_raw
    ADD CONSTRAINT uq_external_att_res_user_date UNIQUE (user_code, kq_date);

CREATE INDEX ix_external_att_res_kq_date ON public.external_attendance_result_raw (kq_date);
CREATE INDEX ix_external_att_res_user_code ON public.external_attendance_result_raw (user_code);

COMMENT ON TABLE public.external_attendance_result_raw IS '外部考勤结果（日结果视图 view_external_att_result）原始镜像。';
COMMENT ON COLUMN public.external_attendance_result_raw.user_code IS '工号（源 user_code）';
COMMENT ON COLUMN public.external_attendance_result_raw.kq_date IS '考勤日期 YYYY-MM-DD（源 kq_date）';
COMMENT ON COLUMN public.external_attendance_result_raw.status_name IS '考勤结果状态（源 status_name）';
COMMENT ON COLUMN public.external_attendance_result_raw.actual_on IS '实际上班时间（源 actual_on）';
COMMENT ON COLUMN public.external_attendance_result_raw.actual_off IS '实际下班时间（源 actual_off）';
