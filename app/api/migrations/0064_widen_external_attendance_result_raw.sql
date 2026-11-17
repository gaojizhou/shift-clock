-- ========================================================================
-- 0064：放宽 external_attendance_result_raw 字段宽度。
--
-- 79 测试库为 SQL_ASCII，varchar 按字节计长；view_external_att_result 中
-- kq_date_show 返回 "2026-06-01 00:00:00"，已超过 0063 初始 varchar(16)。
-- 结果视图以原始镜像为主，除 kq_date 作为业务日期外，其余源字段统一放宽。
-- ========================================================================

ALTER TABLE public.external_attendance_result_raw
    ALTER COLUMN tenant_code TYPE character varying(255),
    ALTER COLUMN dept_name TYPE character varying(255),
    ALTER COLUMN layer_name TYPE character varying(2000),
    ALTER COLUMN hr_status TYPE character varying(255),
    ALTER COLUMN hr_type TYPE character varying(255),
    ALTER COLUMN user_code TYPE character varying(255),
    ALTER COLUMN user_name TYPE character varying(255),
    ALTER COLUMN kq_date_show TYPE character varying(255),
    ALTER COLUMN kq_date_title TYPE character varying(255),
    ALTER COLUMN kqqs TYPE character varying(255),
    ALTER COLUMN status_name TYPE character varying(255),
    ALTER COLUMN lx_type TYPE character varying(255),
    ALTER COLUMN actual_on TYPE character varying(255),
    ALTER COLUMN actual_off TYPE character varying(255),
    ALTER COLUMN quota TYPE character varying(255),
    ALTER COLUMN cqgs TYPE character varying(255),
    ALTER COLUMN kggs TYPE character varying(255),
    ALTER COLUMN cd2 TYPE character varying(255),
    ALTER COLUMN zt2 TYPE character varying(255),
    ALTER COLUMN ot1 TYPE character varying(255),
    ALTER COLUMN ot2 TYPE character varying(255),
    ALTER COLUMN ot3 TYPE character varying(255),
    ALTER COLUMN leave_01 TYPE character varying(255),
    ALTER COLUMN leave_02 TYPE character varying(255),
    ALTER COLUMN leave_03 TYPE character varying(255),
    ALTER COLUMN ext1 TYPE character varying(255),
    ALTER COLUMN ext2 TYPE character varying(255),
    ALTER COLUMN business_01 TYPE character varying(255),
    ALTER COLUMN business_02 TYPE character varying(255);
