-- request_* 审计经 Redis Stream 至少一次投递；event_id 用于消费端幂等落库。
-- 历史及业务操作审计不走队列，因此保持 nullable。
-- fdc:postgresql-concurrent-index
-- migrate.py 在 PostgreSQL 上以 AUTOCOMMIT 执行本文件，并在真实 DDL 上启用
-- 在线并发建索引；SQLite/测试环境仍按普通事务执行。
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS event_id VARCHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_log_event_id
    ON audit_log (event_id)
    WHERE event_id IS NOT NULL;
