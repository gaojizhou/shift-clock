-- ========================================================================
-- 角色中文名追加 code 后缀以便区分：name → 「中文名（code）」
--
-- 背景：role 字典扩到 7 类后（worker/chief/hr/stat/admin/repairer/leader），
--   仅看中文名在某些界面不易与编码对应。本迁移把所有角色中文名统一改成
--   「中文名（code）」格式（如 值班长（chief）），便于一眼区分。
--
-- 用 UPDATE 而非改 0001/0030 源：迁移仅执行一次，已迁库改源不重跑；
--   本条对已迁库直接修正，对全新库也会在最后兜底覆盖。
-- ========================================================================

UPDATE public.role SET name = '操作工（worker）',    updated_at = '2026-06-11 00:00:00+00', updated_by = 'admin' WHERE code = 'worker';
UPDATE public.role SET name = '值班长（chief）',     updated_at = '2026-06-11 00:00:00+00', updated_by = 'admin' WHERE code = 'chief';
UPDATE public.role SET name = '人事专员（hr）',        updated_at = '2026-06-11 00:00:00+00', updated_by = 'admin' WHERE code = 'hr';
UPDATE public.role SET name = '统计员（stat）',      updated_at = '2026-06-11 00:00:00+00', updated_by = 'admin' WHERE code = 'stat';
UPDATE public.role SET name = '系统管理员（admin）',   updated_at = '2026-06-11 00:00:00+00', updated_by = 'admin' WHERE code = 'admin';
UPDATE public.role SET name = '维修工（repairer）', updated_at = '2026-06-11 00:00:00+00', updated_by = 'admin' WHERE code = 'repairer';
UPDATE public.role SET name = '班组长（leader）',    updated_at = '2026-06-11 00:00:00+00', updated_by = 'admin' WHERE code = 'leader';
