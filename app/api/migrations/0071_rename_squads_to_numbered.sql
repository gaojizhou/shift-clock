-- ========================================================================
-- 班组显示名恢复为 1/2/3班
--
-- 编码 SQ1/SQ2/SQ3 保持不变。与 0068 不同：squad_schedule 已改为时段表，
-- 其中 squad=早班/中班/晚班 表示时段，不再跟随班组字典改名。
-- ========================================================================

UPDATE public.squad
SET name = CASE code
    WHEN 'SQ1' THEN '1班'
    WHEN 'SQ2' THEN '2班'
    WHEN 'SQ3' THEN '3班'
    ELSE name
END
WHERE code IN ('SQ1', 'SQ2', 'SQ3');

UPDATE public.quota_rule
SET squad = CASE
    WHEN squad_code = 'SQ1' OR squad = '早班' THEN '1班'
    WHEN squad_code = 'SQ2' OR squad = '中班' THEN '2班'
    WHEN squad_code = 'SQ3' OR squad = '晚班' THEN '3班'
    ELSE squad
END
WHERE squad_code IN ('SQ1', 'SQ2', 'SQ3')
   OR squad IN ('早班', '中班', '晚班');

UPDATE public.quota_template
SET squad = CASE
    WHEN squad_code = 'SQ1' OR squad = '早班' THEN '1班'
    WHEN squad_code = 'SQ2' OR squad = '中班' THEN '2班'
    WHEN squad_code = 'SQ3' OR squad = '晚班' THEN '3班'
    ELSE squad
END
WHERE squad_code IN ('SQ1', 'SQ2', 'SQ3')
   OR squad IN ('早班', '中班', '晚班');

UPDATE public.attendance_record
SET squad = CASE
    WHEN squad_code = 'SQ1' OR squad = '早班' THEN '1班'
    WHEN squad_code = 'SQ2' OR squad = '中班' THEN '2班'
    WHEN squad_code = 'SQ3' OR squad = '晚班' THEN '3班'
    ELSE squad
END
WHERE squad_code IN ('SQ1', 'SQ2', 'SQ3')
   OR squad IN ('早班', '中班', '晚班');

UPDATE public.scan_record
SET squad = CASE
    WHEN squad_code = 'SQ1' OR squad = '早班' THEN '1班'
    WHEN squad_code = 'SQ2' OR squad = '中班' THEN '2班'
    WHEN squad_code = 'SQ3' OR squad = '晚班' THEN '3班'
    ELSE squad
END
WHERE squad_code IN ('SQ1', 'SQ2', 'SQ3')
   OR squad IN ('早班', '中班', '晚班');

UPDATE public.makeup_request
SET squad = CASE
    WHEN squad_code = 'SQ1' OR squad = '早班' THEN '1班'
    WHEN squad_code = 'SQ2' OR squad = '中班' THEN '2班'
    WHEN squad_code = 'SQ3' OR squad = '晚班' THEN '3班'
    ELSE squad
END
WHERE squad_code IN ('SQ1', 'SQ2', 'SQ3')
   OR squad IN ('早班', '中班', '晚班');

