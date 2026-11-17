-- ========================================================================
-- 0058：employee 增加 push_enabled（是否允许钉钉推送）
--
-- 重要：所有钉钉推送(轮班/异常/缺编/测试)发送前必须判断该列为 true 才发送。
-- 存量员工默认 false（严格，需本人/admin 开启或自助导入后自动开启才推送）。
-- 自助导入(import-self) 落库时置 true；手动批量同步/HR单人导入不改此值。
-- ========================================================================

ALTER TABLE public.employee
    ADD COLUMN IF NOT EXISTS push_enabled boolean NOT NULL DEFAULT false;
