-- ========================================================================
-- audit_log 查询索引：操作日志支持按 action 过滤、按 ts 倒序/时间范围筛选
-- （list_audit_logs 已把 action/操作人/白名单下沉到 SQL）。
--
-- 与 models.py 中 AuditLog.action / AuditLog.ts 的 index=True 对齐：
--   * SQLite 单测库经 Base.metadata.create_all 自动建索引；
--   * 生产 PostgreSQL 不走 create_all（表由迁移建），故必须由本迁移补上，
--     否则「下沉 SQL」在生产实际走全表扫描。
-- 命名沿用 SQLAlchemy 约定 ix_<table>_<column>，两端保持一致。
--
-- 注：本仓库迁移执行器把单个 .sql 文件作为一个事务执行；CREATE INDEX CONCURRENTLY
-- 不允许在事务块内运行，故这里用普通 CREATE INDEX。audit_log 体量小，建索引的
-- 短暂写锁可接受。
-- ========================================================================

CREATE INDEX IF NOT EXISTS ix_audit_log_action ON public.audit_log (action);
CREATE INDEX IF NOT EXISTS ix_audit_log_ts ON public.audit_log (ts);
