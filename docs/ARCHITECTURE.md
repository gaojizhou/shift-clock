# 架构说明

## 组件

```text
浏览器（admin / h5）
      │ HTTP + cookie session
      ▼
FastAPI（app/api/app）
  ├── routers/api.py        业务接口（扫码、确认、异常、导出、报表…）
  ├── routers/test_mode.py  本地/演示身份入口（签名门控）
  ├── services/             领域服务（考勤、对账、导出、通知、同步…）
  ├── domain/               纯规则（班次、对账、时区、岗位别名）
  ├── models.py             SQLAlchemy 模型
  └── migrate.py            按序执行 SQL 迁移
      │
      ├── PostgreSQL        主数据 + 业务数据 + 审计
      └── Redis（可选）      请求审计 Stream + worker
```

前端为两个 Vite 应用：

- `app/web/apps/admin`：管理后台（字典、员工、编制、异常、导出、同步）。
- `app/web/apps/h5`：移动端（扫码上岗、记录、负责人日确认）。
- `app/web/packages/api-client`：与 API 同步的类型化客户端。

## 数据流

1. **扫码上岗**：H5 解析工作点二维码 → `POST /api/v1/scan` → 校验员工、编制、月锁后写入
   `attendance_record`，并触发当日对账。
2. **负责人确认**：`h5` 日确认页读取当日名单与异常证据 → 负责人处理/锁定 →
   `chief_confirm` 记录确认状态。
3. **异常处理**：异常工作台聚合缺失/冲突/半班/加班等，产出处理记录与审计。
4. **导出**：`services/export_service` 生成 Excel；`monthly_export_lock` 控制已封账月份。
5. **可选同步**：外部考勤源（MySQL 只读视图）→ `dingtalk_attendance_raw` /
   `external_attendance_result_raw` → 转换为对账数据；未配置时相关任务不启动。

## 认证与会话

- 浏览器只保存 `fdc_session` cookie；会话映射存 `app_session` 表，支持滑动续期。
- 生产登录：钉钉免登（`/api/v1/dingtalk/login`）或部署方接入的身份提供方。
- 系统管理员 TOTP 入口（`/api/v1/auth/admin-login`）在配置 `FDC_ADMIN_TOTP_SECRET` 后启用。
- 演示/测试入口（`/auth/login`、`X-Test-User`/`X-Test-Role`）仅在 `FDC_TEST_MODE=1` 时生效。
- `/h5/test/auth`、`/h5/test/employees` 由 `FDC_TEST_LOGIN_SECRET` 签名门控，未配置即 403。

## 权限模型

- 角色：`worker`（操作工）/ `chief`（值班长）/ `hr`（人事专员）/ `stat`（统计员）/ `admin`（系统管理员）/ `repairer`（维修工）/ `leader`（班组长）。
- `admin` 为超级权限；人事专员/统计员按服务端地区范围收窄数据。
- 值班长只能操作其绑定工作点；员工只能为自己扫码，管理角色可代签。
- 所有敏感操作写 `audit_log`。

## 部署形态

- 单 API 进程 + 单 worker + PostgreSQL + 可选 Redis（Compose）。
- API 与 worker 启动时执行 `python -m app.migrate --skip-online-index`。
- 静态前端由 API 进程按 `STATIC_ADMIN` / `STATIC_H5` 挂载；留空则只提供 API。
