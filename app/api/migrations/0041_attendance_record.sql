-- ========================================================================
-- 表：attendance_record（统一工时记录表）
--
-- 工时制改造：替代 scan_record + makeup_request 两张旧表，成为唯一考勤主表。
--   · 工时口径：占比制(整班1.0/半班0.5) → 工时制(整班8h/半班4h)，字段 work_hours。
--   · 三轴区分：record_kind(出勤/加班) × source(扫码/代签/补录) × status(待审/active/拒绝/已取消)。
--   · 补录/代签并入：一条补录=status='待审'的记录，审批通过 status→'active'，不再另建记录。
--   · 加班维度(record_kind/region_code/settled_hours)已预留，加班业务逻辑后续迭代实现。
-- 旧表 scan_record / makeup_request 保留弃用（见 0042），仅作回滚与审计。
-- ========================================================================

CREATE TABLE public.attendance_record (
    id integer NOT NULL,                                    -- 自增主键（并表后不沿用旧 id）
    emp_id character varying(32) NOT NULL,                  -- 员工工号（employee.emp_id）
    furnace_id integer NOT NULL,                            -- 工作点外键 → furnace.id
    squad character varying(16),                            -- 班次名称；NULL=不分班岗
    squad_code character varying(16),                       -- 班次字典外键 → squad.code
    post character varying(64) NOT NULL,                    -- 岗位名称（展示冗余）
    position_code character varying(16),                    -- 职位字典外键 → position.code
    employ_type character varying(16) NOT NULL,             -- 用工类型：正式/实习/待岗（影响编制占用口径）
    region_code character varying(16),                      -- 班组外键 → region.code；加班必填，出勤可空
    scan_time timestamp with time zone,                     -- 扫码/加班卡时刻（带时区）；待审补录可空
    source character varying(16) NOT NULL,                  -- 来源：扫码 / 代签 / 补录
    status character varying(16) NOT NULL,                  -- 状态：待审 / active(有效) / 拒绝 / 已取消
    natual_date character varying(10) NOT NULL,             -- 归属自然日 YYYY-MM-DD（跨午夜班次归前一日）
    work_hours double precision DEFAULT '8'::double precision NOT NULL, -- 出勤工时：整班=8.0，半班=4.0（算薪占用口径）
    record_kind character varying(16) DEFAULT '出勤'::character varying NOT NULL, -- 记录类型：出勤 / 班内加班 / 延时加班
    settled_hours double precision,                         -- 延时加班对账核定后工时；出勤/班内 = work_hours
    recon_status character varying(16),                     -- 延时加班对账状态：待拉数据/已覆盖/缺下班卡/已补卡
    reason text,                                            -- 申请原因（补录/代签自由文本）
    reviewed_by character varying(64),                      -- 审批人工号（补录/代签审批，或加班值班长前置确认）
    reviewed_at timestamp with time zone,                   -- 审批/确认时刻
    legacy_table character varying(32),                     -- 迁移来源表名（scan_record / makeup_request），审计溯源
    legacy_id integer                                       -- 迁移来源原始 id，审计溯源
);

CREATE SEQUENCE public.attendance_record_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.attendance_record_id_seq OWNED BY public.attendance_record.id;

ALTER TABLE ONLY public.attendance_record ALTER COLUMN id SET DEFAULT nextval('public.attendance_record_id_seq'::regclass);

ALTER TABLE ONLY public.attendance_record
    ADD CONSTRAINT attendance_record_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.attendance_record
    ADD CONSTRAINT attendance_record_furnace_id_fkey FOREIGN KEY (furnace_id) REFERENCES public.furnace(id);

ALTER TABLE ONLY public.attendance_record
    ADD CONSTRAINT attendance_record_position_code_fkey FOREIGN KEY (position_code) REFERENCES public."position"(code);

ALTER TABLE ONLY public.attendance_record
    ADD CONSTRAINT attendance_record_squad_code_fkey FOREIGN KEY (squad_code) REFERENCES public.squad(code);

ALTER TABLE ONLY public.attendance_record
    ADD CONSTRAINT attendance_record_region_code_fkey FOREIGN KEY (region_code) REFERENCES public.region(code);

-- 唯一约束：同一员工同一自然日只能有一条有效(active)出勤记录（沿用旧 uq_scan_active_emp_date 语义）。
-- 仅约束 record_kind='出勤' 且 status='active'：待审/拒绝/已取消 不参与；加班记录不占此唯一槽。
CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_active_emp_date
    ON public.attendance_record (emp_id, natual_date)
    WHERE (status = 'active' AND record_kind = '出勤');

-- 常用查询索引
CREATE INDEX IF NOT EXISTS ix_attendance_emp_date ON public.attendance_record (emp_id, natual_date);
CREATE INDEX IF NOT EXISTS ix_attendance_furnace_date ON public.attendance_record (furnace_id, natual_date);
CREATE INDEX IF NOT EXISTS ix_attendance_status_kind ON public.attendance_record (status, record_kind);

-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.attendance_record IS '统一工时记录表（出勤/加班/补录代签）。替代 scan_record + makeup_request。';
COMMENT ON COLUMN public.attendance_record.work_hours IS '出勤工时：整班=8.0，半班=4.0（算薪占用口径）';
COMMENT ON COLUMN public.attendance_record.record_kind IS '记录类型：出勤 / 班内加班 / 延时加班';
COMMENT ON COLUMN public.attendance_record.source IS '来源：扫码 / 代签 / 补录';
COMMENT ON COLUMN public.attendance_record.status IS '状态：待审 / active(有效) / 拒绝 / 已取消';
COMMENT ON COLUMN public.attendance_record.region_code IS '班组外键 → region.code；加班必填，出勤可空';
COMMENT ON COLUMN public.attendance_record.settled_hours IS '延时加班对账核定后工时；出勤/班内 = work_hours';
COMMENT ON COLUMN public.attendance_record.reviewed_by IS '审批人工号（补录/代签审批，或加班值班长前置确认）';
COMMENT ON COLUMN public.attendance_record.legacy_table IS '迁移来源表名，审计溯源';
COMMENT ON COLUMN public.attendance_record.legacy_id IS '迁移来源原始 id，审计溯源';

-- 本表不预置数据：历史数据由 0042 从旧表迁入，运行期产生新数据
