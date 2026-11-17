-- ========================================================================
-- 工作点编制「扫码可选」从布尔升级为「可见角色多选」
--
-- 背景：原 quota_template.selectable（布尔）只表达「工人能否扫码选该岗」。
--   现改为按角色控制可见/可选——每个岗位维护一组「可见角色」(role.code 逗号拼接)，
--   扫码上岗时仅当「当前用户角色 ∈ 该岗可见角色」才显示并可选。
--
-- 存储：visible_roles VARCHAR，逗号拼接 role.code（如 'worker' / 'worker,leader'）；
--   空串=无人可扫码选。
--
-- 默认映射（旧 selectable → 角色集）：分班岗→'worker'；不分班岗(值班长/维修工)→'chief'。
--
-- 影响两表：
--   quota_template：新增 visible_roles，按 squadless 回填，删除 selectable 列。
--   selectable_post（工作点/地区级白名单，扫码实际读取）：新增 visible_roles；
--     存量行均为工人可选岗 → 'worker'。
-- ========================================================================

-- 1) quota_template：加列 → 回填 → 删旧 selectable 列
ALTER TABLE public.quota_template ADD COLUMN visible_roles character varying(128) NOT NULL DEFAULT '';
UPDATE public.quota_template
   SET visible_roles = CASE WHEN squadless THEN 'chief' ELSE 'worker' END;
ALTER TABLE public.quota_template DROP COLUMN selectable;
COMMENT ON COLUMN public.quota_template.visible_roles IS '可见角色：role.code 逗号拼接；扫码时该组角色才能看到并选择此岗（空=无人可选）。';

-- 2) selectable_post：加列 + 存量回填为 worker（均为工人可选岗）
ALTER TABLE public.selectable_post ADD COLUMN visible_roles character varying(128) NOT NULL DEFAULT 'worker';
COMMENT ON COLUMN public.selectable_post.visible_roles IS '可见角色：role.code 逗号拼接；扫码时该组角色才能看到并选择此岗（空=无人可选）。';
