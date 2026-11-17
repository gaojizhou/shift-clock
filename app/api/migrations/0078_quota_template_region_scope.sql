-- 编制模板由“全公司每设备类型一套”改为“每地区、每设备类型一套”。
-- region=NULL 仍保留为 admin 管理的全局模板空间。
ALTER TABLE public.quota_template
    DROP CONSTRAINT IF EXISTS quota_template_furnace_type_code_post_squad_key;

ALTER TABLE public.quota_template
    ADD CONSTRAINT quota_template_region_furnace_type_post_squad_key
    UNIQUE (region, furnace_type_code, post, squad);
