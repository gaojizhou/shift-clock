-- 多地区收藏的 HR/统计账号回到「可自助选择」状态。
--
-- 0076 把「有任意 region 收藏」的 hr/stat 回填为已确认（region_confirmed_at=now()），
-- 与多地区 409 门禁叠加后，这类账号所有业务接口 409、POST /me/region 也 409，
-- 完全不可用且无法自助恢复。此处清空其确认标记：账号首次进入选区页时，
-- 可从已有地区收藏中收窄确认其一（服务端只允许收窄、不允许扩权）。
-- 恰有一个地区收藏的账号不受影响，保持已确认。
UPDATE public.employee e
SET region_confirmed_at = NULL
WHERE e.role IN ('hr', 'stat')
  AND e.region_confirmed_at IS NOT NULL
  AND (
      SELECT COUNT(*)
      FROM public.employee_favorite f
      WHERE f.emp_id = e.emp_id
        AND f.kind = 'region'
  ) > 1;
