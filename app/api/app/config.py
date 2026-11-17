from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://fdc:fdc@localhost:5432/fdc"
    # 单进程 API 的 PostgreSQL 连接池：50 个常驻槽位 + 50 个突发槽位，
    # 获取连接最多等待 5 秒。测试使用 SQLite 时不会应用这些 QueuePool 参数。
    fdc_db_pool_size: int = Field(default=50, ge=1, le=100)
    fdc_db_max_overflow: int = Field(default=50, ge=0, le=100)
    fdc_db_pool_timeout_seconds: float = Field(default=5.0, ge=1.0, le=60.0)
    # 重报表与劳勤列表共用单进程容量；正式部署只有一个 API worker。
    fdc_reports_paused: bool = False
    fdc_report_concurrency: int = Field(default=1, ge=1, le=4)
    fdc_report_max_followers: int = Field(default=4, ge=0, le=16)
    fdc_report_follower_timeout_seconds: float = Field(default=2.0, ge=0.1, le=5.0)
    fdc_report_timeout_seconds: float = Field(default=60.0, ge=1.0, le=180.0)
    fdc_report_statement_timeout_ms: int = Field(default=10000, ge=100, le=60000)
    fdc_report_lock_timeout_ms: int = Field(default=2000, ge=100, le=10000)
    fdc_report_idle_transaction_timeout_ms: int = Field(default=15000, ge=1000, le=60000)
    fdc_report_max_source_rows: int = Field(default=100000, ge=100, le=500000)
    fdc_report_max_output_cells: int = Field(default=250000, ge=100, le=1000000)
    # 高频 request_* 请求审计通过 Redis Stream 削峰，再由独立 worker 批量写库。
    # 留空保持同步写入（单元测试/无 Redis 的本地环境兼容）；部署 compose 会显式启用。
    fdc_request_audit_redis_url: str = ""
    fdc_request_audit_stream: str = "fdc:request-audit"
    fdc_request_audit_group: str = "fdc-request-audit-writers"
    # API 进程内 L1 有界队列：请求只做 put_nowait，后台协程批量写入 Redis Stream。
    fdc_request_audit_memory_queue_size: int = Field(default=5000, ge=100, le=100000)
    fdc_request_audit_memory_queue_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=1024 * 1024,
        le=512 * 1024 * 1024,
    )
    fdc_request_audit_redis_writer_count: int = Field(default=2, ge=1, le=16)
    fdc_request_audit_redis_batch_size: int = Field(default=200, ge=1, le=1000)
    fdc_request_audit_redis_flush_interval_ms: int = Field(default=25, ge=1, le=1000)
    fdc_request_audit_shutdown_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    # 连接、普通命令和整批 pipeline 分开限时。批量写入不能复用 200ms 级连接超时，
    # 否则 Redis AOF/fork 抖动时会把一个已执行的批次误判为失败并频繁重投。
    fdc_request_audit_redis_connect_timeout_seconds: float = Field(
        default=0.5,
        ge=0.05,
        le=5.0,
    )
    fdc_request_audit_redis_command_timeout_seconds: float = Field(
        default=2.0,
        ge=0.1,
        le=10.0,
    )
    fdc_request_audit_redis_pipeline_timeout_seconds: float = Field(
        default=2.0,
        ge=0.2,
        le=30.0,
    )
    # 按单条最坏约 4.8KB 估算，25000 条约 120MB，为 256MB Redis 的
    # noeviction + AOF rewrite 预留足够内存。
    fdc_request_audit_stream_maxlen: int = Field(default=25000, ge=1000, le=1000000)
    fdc_request_audit_worker_heartbeat_ttl_seconds: int = Field(default=15, ge=5, le=300)
    # Redis → PostgreSQL worker 的消费批次配置。
    fdc_request_audit_batch_size: int = Field(default=200, ge=1, le=1000)
    fdc_request_audit_block_ms: int = Field(default=1000, ge=100, le=10000)
    fdc_request_audit_flush_interval_ms: int = Field(default=500, ge=10, le=5000)
    fdc_request_audit_claim_idle_ms: int = Field(default=30000, ge=1000, le=3600000)
    # 仅控制管理后台测试环境标识/配色，不改变鉴权或测试登录能力。
    fdc_admin_test_banner: bool = False
    # 演示/测试身份入口（X-Test-User / X-Test-Role、/auth/login）。默认关闭；
    # 本地演示用环境变量 FDC_TEST_MODE=1 显式开启。正式部署必须保持关闭。
    fdc_test_mode: bool = False
    fdc_auth_mode: str = "mock"
    fdc_notify_mode: str = "mock"
    fdc_escalation_tz: str = "Asia/Shanghai"
    fdc_attendance_sync_scheduler_enabled: bool = True
    fdc_proxy_signin_monthly_limit: int = 3  # initial default; runtime value stored in app_setting
    # 可选：演示数据目录（仅 demo seed 使用）。正式部署留空。
    fdc_csv_dir: str = ""
    # 可选：前端构建产物目录。留空表示不挂载静态站点（API 仍可用）。
    static_admin: str = ""
    static_h5: str = ""
    port: int = 3000
    # 可选：外部考勤源（MySQL 只读视图）。未配置 host 时集成视为未启用，不发起任何连接。
    attendance_mysql_host: str = ""
    attendance_mysql_port: int = 3306
    attendance_mysql_user: str = ""
    attendance_mysql_password: str = ""
    attendance_mysql_db: str = ""
    attendance_mysql_view: str = "view_external_att_data"
    # 可选：外部考勤结果视图名。仅当外部考勤源已配置时使用。
    attendance_result_view: str = "view_external_att_result"
    # 钉钉微应用凭据（免登 / 用户信息）。未配置时调试接口优雅降级，仅回显 authCode。
    dingtalk_corp_id: str = ""
    dingtalk_agent_id: str = ""
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""
    # 超级管理员两步验证 TOTP base32 密钥（env FDC_ADMIN_TOTP_SECRET）。为空 = 入口关闭。
    fdc_admin_totp_secret: str = ""
    # 测试登录签名密钥（env FDC_TEST_LOGIN_SECRET）。本地启动器持同值签名 /h5/test/auth。
    # 为空 = 签名登录入口关闭（生产挂着也无风险）；配置后即便 FDC_TEST_MODE=0 也可签名登录。
    fdc_test_login_secret: str = ""
    # 登录会话滑动过期时间，默认 7 天。后端 DB session 与浏览器 cookie 使用同一 TTL。
    fdc_session_ttl_seconds: int = 7 * 24 * 60 * 60

    class Config:
        # 同时支持在 app/api/ 下运行（读 app/.env）与在 app/ 下运行（读 .env）。
        env_file = ("../.env", ".env")
        extra = "ignore"


settings = Settings()
