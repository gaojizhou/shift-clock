-- 将班次显示名从 1/2/3 班调整为早/中/晚班。
-- 编码 SQ1/SQ2/SQ3 保持不变，避免破坏外键、排序和历史关联。

UPDATE public.squad
SET name = CASE code
    WHEN 'SQ1' THEN '早班'
    WHEN 'SQ2' THEN '中班'
    WHEN 'SQ3' THEN '晚班'
    ELSE name
END
WHERE code IN ('SQ1', 'SQ2', 'SQ3');

UPDATE public.squad_schedule
SET squad = CASE squad_code
    WHEN 'SQ1' THEN '早班'
    WHEN 'SQ2' THEN '中班'
    WHEN 'SQ3' THEN '晚班'
    ELSE squad
END
WHERE squad_code IN ('SQ1', 'SQ2', 'SQ3')
   OR squad IN ('1班', '2班', '3班');

UPDATE public.quota_rule
SET squad = CASE
    WHEN squad_code = 'SQ1' OR squad = '1班' THEN '早班'
    WHEN squad_code = 'SQ2' OR squad = '2班' THEN '中班'
    WHEN squad_code = 'SQ3' OR squad = '3班' THEN '晚班'
    ELSE squad
END
WHERE squad_code IN ('SQ1', 'SQ2', 'SQ3')
   OR squad IN ('1班', '2班', '3班');

UPDATE public.quota_template
SET squad = CASE
    WHEN squad_code = 'SQ1' OR squad = '1班' THEN '早班'
    WHEN squad_code = 'SQ2' OR squad = '2班' THEN '中班'
    WHEN squad_code = 'SQ3' OR squad = '3班' THEN '晚班'
    ELSE squad
END
WHERE squad_code IN ('SQ1', 'SQ2', 'SQ3')
   OR squad IN ('1班', '2班', '3班');

UPDATE public.attendance_record
SET squad = CASE
    WHEN squad_code = 'SQ1' OR squad = '1班' THEN '早班'
    WHEN squad_code = 'SQ2' OR squad = '2班' THEN '中班'
    WHEN squad_code = 'SQ3' OR squad = '3班' THEN '晚班'
    ELSE squad
END
WHERE squad_code IN ('SQ1', 'SQ2', 'SQ3')
   OR squad IN ('1班', '2班', '3班');

UPDATE public.scan_record
SET squad = CASE
    WHEN squad_code = 'SQ1' OR squad = '1班' THEN '早班'
    WHEN squad_code = 'SQ2' OR squad = '2班' THEN '中班'
    WHEN squad_code = 'SQ3' OR squad = '3班' THEN '晚班'
    ELSE squad
END
WHERE squad_code IN ('SQ1', 'SQ2', 'SQ3')
   OR squad IN ('1班', '2班', '3班');

UPDATE public.makeup_request
SET squad = CASE
    WHEN squad_code = 'SQ1' OR squad = '1班' THEN '早班'
    WHEN squad_code = 'SQ2' OR squad = '2班' THEN '中班'
    WHEN squad_code = 'SQ3' OR squad = '3班' THEN '晚班'
    ELSE squad
END
WHERE squad_code IN ('SQ1', 'SQ2', 'SQ3')
   OR squad IN ('1班', '2班', '3班');
