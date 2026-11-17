-- 登录会话增加过期时间，避免被盗 session 永久有效。
-- 现有会话保留一个默认 TTL 窗口，后续请求按后端滑动续期。

ALTER TABLE public.app_session
    ADD COLUMN IF NOT EXISTS expires_at timestamp with time zone;

UPDATE public.app_session
SET expires_at = now() + interval '7 days'
WHERE expires_at IS NULL;

ALTER TABLE public.app_session
    ALTER COLUMN expires_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_app_session_expires_at
    ON public.app_session (expires_at);
