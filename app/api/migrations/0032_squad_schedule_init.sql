-- ========================================================================
-- 初始数据：squad_schedule 班次时间表（0017 仅建表未灌数据，导致「考勤对照」
-- 取不到各班起止时间，班次背景带/班次段高亮无法绘制——本迁移补齐）。
--
-- 每地区 1/2/3 班起止（HH:MM，end<=start 表示跨午夜）：
--   示例厂区A(REG01): 1班 02:00-10:00 / 2班 10:00-18:00 / 3班 18:00-02:00(次日)
--   示例厂区B(REG02):   1班 01:00-09:00 / 2班 09:00-17:00 / 3班 17:00-01:00(次日)
-- 执行到 0072 后，班组表 final 口径为 早班/中班/晚班（squad_code 为空）。
-- ========================================================================

INSERT INTO public.squad_schedule
    (id, region, region_code, squad, squad_code, start_time, end_time,
     created_at, updated_at, created_by, updated_by, deleted)
VALUES
    (1, '示例厂区A', 'REG01', '1班', 'SQ1', '02:00', '10:00', now(), now(), 'admin', 'admin', false),
    (2, '示例厂区A', 'REG01', '2班', 'SQ2', '10:00', '18:00', now(), now(), 'admin', 'admin', false),
    (3, '示例厂区A', 'REG01', '3班', 'SQ3', '18:00', '02:00', now(), now(), 'admin', 'admin', false),
    (4, '示例厂区B',   'REG02', '1班', 'SQ1', '01:00', '09:00', now(), now(), 'admin', 'admin', false),
    (5, '示例厂区B',   'REG02', '2班', 'SQ2', '09:00', '17:00', now(), now(), 'admin', 'admin', false),
    (6, '示例厂区B',   'REG02', '3班', 'SQ3', '17:00', '01:00', now(), now(), 'admin', 'admin', false);

SELECT pg_catalog.setval('public.squad_schedule_id_seq', 6, true);
