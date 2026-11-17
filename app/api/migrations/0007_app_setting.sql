-- ========================================================================
-- 表：app_setting
-- 运行时可改的键值参数表（无需改代码/重启的开关与阈值）。
-- 当前键：proxy_signin_monthly_limit（代签每人每月默认上限，管理页「代签额度」可改）。
-- ========================================================================

CREATE TABLE public.app_setting (
    key character varying(64) NOT NULL,                     -- 参数键（主键），如 proxy_signin_monthly_limit
    value character varying(256) NOT NULL                   -- 参数值（字符串存储，使用方自行转型）
);

ALTER TABLE ONLY public.app_setting
    ADD CONSTRAINT app_setting_pkey PRIMARY KEY (key);
-- 落库注释（数据库工具中可见字段说明）
COMMENT ON TABLE public.app_setting IS '运行时可改的键值参数表（无需改代码/重启的开关与阈值）。';
COMMENT ON COLUMN public.app_setting.key IS '参数键（主键），如 proxy_signin_monthly_limit';
COMMENT ON COLUMN public.app_setting.value IS '参数值（字符串存储，使用方自行转型）';

-- 初始无数据（运行期由业务维护）
