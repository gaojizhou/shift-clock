-- 通讯录同步根部门增加基础地区映射；转换花名册时用于写员工常用地区。

ALTER TABLE public.roster_root_dept
    ADD COLUMN IF NOT EXISTS region_code varchar(16);

-- 已有行按业务含义回填；其余同步范围由管理员在页面选择。
UPDATE public.roster_root_dept
SET region_code = 'REG01'
WHERE region_code IS NULL AND strpos(label, '示例厂区A') > 0;

UPDATE public.roster_root_dept
SET region_code = 'REG02'
WHERE region_code IS NULL AND strpos(label, '示例厂区B') > 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'roster_root_dept_region_code_fkey'
    ) THEN
        ALTER TABLE public.roster_root_dept
            ADD CONSTRAINT roster_root_dept_region_code_fkey
            FOREIGN KEY (region_code) REFERENCES public.region(code);
    END IF;
END $$;

COMMENT ON COLUMN public.roster_root_dept.region_code
    IS '根部门对应基础地区代码；转换花名册时写入员工常用地区';
