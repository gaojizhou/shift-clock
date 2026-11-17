-- 员工在职/离职状态与 deleted 软删除分离。
-- 存量员工默认在职；如已有通讯录原始数据，按最新的明确 active 值回填。

ALTER TABLE public.employee
    ADD COLUMN employment_status varchar(16) NOT NULL DEFAULT 'active';

WITH latest_status AS (
    SELECT DISTINCT ON (TRIM(COALESCE(NULLIF(job_number, ''), userid)))
        TRIM(COALESCE(NULLIF(job_number, ''), userid)) AS emp_id,
        userid,
        active
    FROM public.dingtalk_employee_raw
    WHERE active IS NOT NULL
      AND TRIM(COALESCE(NULLIF(job_number, ''), userid)) <> ''
    ORDER BY
        TRIM(COALESCE(NULLIF(job_number, ''), userid)),
        synced_at DESC,
        id DESC
)
UPDATE public.employee AS employee
SET employment_status = CASE WHEN latest_status.active THEN 'active' ELSE 'departed' END
FROM latest_status
WHERE employee.emp_id = latest_status.emp_id
   OR employee.dd_user_id = latest_status.userid;

COMMENT ON COLUMN public.employee.employment_status
    IS '人事在职状态：active=在职，departed=离职；与 deleted 软删除无关';
