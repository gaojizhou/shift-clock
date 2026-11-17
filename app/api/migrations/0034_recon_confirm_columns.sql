-- ========================================================================
-- recon_result 增列：HR/值班长 逐条确认（三态：通过/不通过/未确认，相互独立）。
-- 异常工作台展示全量对账记录，HR 与值班长各自对每条做确认；自动一致默认两者「通过」。
-- ========================================================================

ALTER TABLE public.recon_result
    ADD COLUMN hr_status character varying(16) NOT NULL DEFAULT '未确认';      -- HR 确认：通过/不通过/未确认
ALTER TABLE public.recon_result
    ADD COLUMN hr_by character varying(64);                                    -- HR 确认人
ALTER TABLE public.recon_result
    ADD COLUMN hr_at timestamp with time zone;                                 -- HR 确认时间（UTC）
ALTER TABLE public.recon_result
    ADD COLUMN chief_status character varying(16) NOT NULL DEFAULT '未确认';   -- 值班长确认：通过/不通过/未确认
ALTER TABLE public.recon_result
    ADD COLUMN chief_by character varying(64);                                 -- 值班长确认人
ALTER TABLE public.recon_result
    ADD COLUMN chief_at timestamp with time zone;                             -- 值班长确认时间（UTC）

COMMENT ON COLUMN public.recon_result.hr_status IS 'HR 确认：通过/不通过/未确认（与值班长确认相互独立）';
COMMENT ON COLUMN public.recon_result.hr_by IS 'HR 确认人（工号）';
COMMENT ON COLUMN public.recon_result.hr_at IS 'HR 确认时间（UTC）';
COMMENT ON COLUMN public.recon_result.chief_status IS '值班长确认：通过/不通过/未确认（与 HR 确认相互独立）';
COMMENT ON COLUMN public.recon_result.chief_by IS '值班长确认人（工号）';
COMMENT ON COLUMN public.recon_result.chief_at IS '值班长确认时间（UTC）';
