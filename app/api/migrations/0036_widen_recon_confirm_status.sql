-- ========================================================================
-- 加宽 recon_result 的确认状态列：hr_status 新增「值班长确认后通过」(7 汉字)，
-- 线上库为 SQL_ASCII（varchar 按字节计），7 汉字=21 字节 > 16 → 截断报错。
-- 加宽到 48（=16 汉字）兜底；models.py 同步 String(48)。
-- ========================================================================

ALTER TABLE public.recon_result ALTER COLUMN hr_status TYPE character varying(48);
ALTER TABLE public.recon_result ALTER COLUMN chief_status TYPE character varying(48);
