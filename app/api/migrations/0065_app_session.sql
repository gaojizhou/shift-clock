CREATE TABLE IF NOT EXISTS public.app_session (
    session_id character varying(64) PRIMARY KEY,
    userid character varying(64) NOT NULL,
    role character varying(32) NOT NULL,
    is_super boolean NOT NULL DEFAULT false,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_app_session_userid ON public.app_session (userid);
