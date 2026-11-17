# 三班倒打卡系统（shift-clock）

面向三班倒生产现场的业务信息系统，采用 **FastAPI + React + TypeScript + PostgreSQL** 构建，
提供移动端扫码打卡和管理后台，覆盖“员工上岗 → 负责人确认 → 异常核对 → 月度封账与考勤导出”的业务流程。

项目围绕多角色协作、跨系统数据核对和业务记录追溯展开，包含数据库建模、后端接口、前端交互、
外部系统适配、权限控制、审计记录及自动化回归。适合用于了解业务信息系统从规则实现到部署验证的工程实践。

系统把一线员工、现场负责人和人事管理人员的工作串联在同一套流程中：员工通过工作点二维码进入打卡页面，
负责人集中查看并确认出勤，人事人员统一核对异常、封账和导出报表，减少纸质登记、表格流转与反复核对。
移动端面向现场操作，管理后台面向集中处理，让不同角色各有清晰的工作入口。

除了记录“谁来上班”，系统还将岗位编制、半班与加班、补录代签、异常处理和确认记录关联起来，
方便沿着业务记录查清问题、跟进处理。对于希望将三班倒考勤从分散登记转为统一管理的团队，
它提供了一套流程连贯、记录可查、支持私有化部署的系统基础，并可继续扩展外部考勤与组织管理集成。

## 系统研发与工程实践

| 方向 | 项目中的具体实现 |
| --- | --- |
| 业务建模 | 区分员工、角色、班组、班次时段、工作点和岗位编制，组织扫码、补录、确认、异常与封账流程 |
| 后端研发 | 使用 FastAPI 提供 HTTP API，SQLAlchemy 管理数据访问，领域规则与业务服务分层组织 |
| 前端研发 | 使用 React、TypeScript 和 Vite 构建管理后台与移动端，独立维护共享 API 客户端 |
| 数据库设计 | PostgreSQL 保存主数据、出勤与审计；通过唯一约束、索引和带校验和的 SQL 迁移维护数据结构 |
| 系统集成 | 提供钉钉免登、通讯录与推送适配，以及外部 MySQL 考勤源的数据同步与对账逻辑 |
| 权限与追溯 | 基于角色及工作点／地区范围控制操作，使用服务端会话、管理员 TOTP 入口和操作审计 |
| 报表与资源管理 | 提供 Excel 导出、报表快照、导出锁及请求审计队列；Redis Stream 配合独立 worker 处理审计写入 |
| 工程验证 | pytest 覆盖后端规则与接口，前端执行类型检查和构建，Playwright 用例覆盖关键页面流程；保留 CI 与 pre-push 回归入口 |
| 部署交付 | 提供 Docker 多阶段构建、Compose 编排、健康检查及独立的合成演示数据初始化命令 |

项目主实现语言为 **Python 和 TypeScript**。当前版本适合作为业务信息系统研发的实践项目展示；
正式交付前仍需完成 PostgreSQL 空库迁移、容器构建与端到端运行验收，详见下文的运行与能力边界。

## 适用场景

- 早/中/晚三班倒、存在跨午夜班次的生产现场。
- 需要「扫码上岗 → 负责人确认 → 异常处理 → 月结导出」闭环的场景。
- 希望用本地可部署方案替代纸质/表格考勤的团队。

## 已实现

- 员工与角色（操作工 / 值班长 / 人事专员 / 统计员 / 系统管理员 / 维修工 / 班组长）。
- 工作点二维码、地区/班组/岗位/编制维护。
- 早/中/晚时段、跨午夜班次、班次窗口配置。
- 扫码上岗、代签、补录、半天/整班工时、加班记录。
- 负责人日确认、异常工作台、月度封账。
- 考勤对照（扫码 vs 外部考勤源）、异常证据与处理记录。
- 考勤导出（Excel）、报表快照与导出锁。
- 角色权限校验、请求审计、登录会话。
- 可选集成：钉钉免登/通讯录/推送、外部考勤 MySQL 只读视图（未配置时不发起连接）。

## 未实现 / 边界

- 不是通用薪资引擎，不计算工资。
- 不承诺任意国家/时区；首版固定 `Asia/Shanghai`。
- 不内置轮班排班算法：班组是人员组织，班次时段是时间窗口，二者独立。
- 多租户 SaaS、任意轮班算法不在首版范围。
- 普通用户正式登录目前以钉钉免登为主；本地账户登录和可视化演示身份选择尚未提供，自有身份提供方需另行接入。
- 跨夜班次的业务日期、缺省日期与月末归属仍需专项验证，不能仅凭班次时间配置推定结果正确。
- PostgreSQL 空库全量迁移、Docker 镜像构建与完整 E2E 尚待验收；已有测试用例不等于整套部署已验证通过。

## 技术栈

- 后端：Python 3.11+ / FastAPI / SQLAlchemy / PostgreSQL / Redis（审计队列可选）。
- 前端：React + TypeScript + Vite（`admin` 管理后台、`h5` 移动端、`api-client`）。
- 测试：pytest（内存 SQLite）、Playwright（E2E）。
- 容器：Docker / Docker Compose。

## 目录结构

```text
shift-clock/
├── README.md
├── CONTRIBUTING.md
├── SECURITY.md
├── LICENSE
├── AGENTS.md
├── .env.example
├── .gitignore
├── .dockerignore
├── .github/workflows/        # 通用回归 CI
├── app/
│   ├── api/                  # FastAPI 主实现、迁移、测试
│   ├── web/                  # admin、h5、api-client 与锁文件
│   ├── e2e/                  # Playwright
│   ├── Dockerfile
│   └── docker-compose.yml
├── examples/                 # 合成数据/集成示例
├── scripts/                  # 初始化、验证、回归
└── docs/                     # 架构、业务规则、回归与部署说明
```

## 快速开始（本地）

前置：Python 3.11+、Node 22+、pnpm、PostgreSQL 16（或 Docker）。

```bash
# 1) 后端依赖
cd app/api
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
# 可选：按已验证版本安装（可复现）
# .venv/bin/pip install -r requirements.lock

# 2) 数据库：按需修改 DATABASE_URL
cp ../.env.example ../.env
.venv/bin/python -m app.migrate           # 建表 + 基础字典
.venv/bin/python -m app.seed              # 可选：合成演示数据

# 3) 启动 API
.venv/bin/uvicorn app.main:app --reload --port 3000

# 4) 前端
cd ../web
pnpm install --frozen-lockfile
pnpm -r build
```

也可以直接用 Compose：

```bash
docker compose -f app/docker-compose.yml up -d --build
docker compose -f app/docker-compose.yml run --rm api python -m app.seed   # 可选演示数据
```

演示模式开启方式：`FDC_TEST_MODE=1`。此时可使用 `/auth/login` 或 `X-Test-User`/`X-Test-Role`。
**正式部署必须保持 `FDC_TEST_MODE=0`。**

## 演示身份

`python -m app.seed` 会创建明显虚构的合成数据（不含真实姓名/身份证/手机号/钉钉 ID）：

| 工号 | 姓名 | 角色 |
| --- | --- | --- |
| `admin` | 系统管理员 | 系统管理员（系统账号） |
| `DEMO001`…`DEMO006` | 演示操作工一…六 | 操作工 |
| `DEMO101` / `DEMO102` | 演示值班长A / 演示值班长B | 值班长 |
| `DEMO201` | 演示人事专员 | 人事专员 |
| `DEMO301` | 演示统计员 | 统计员 |

演示环境可用 `/auth/login`（`{"userid":"DEMO001"}`）登录；正式环境请接入钉钉免登或自有身份提供方。

## 三班规则

- 班组（1/2/3班）是人员组织；班次时段（早/中/晚）是时间窗口，二者不要混用。
- 时段起止 `end <= start` 表示跨午夜；业务日期以归属日计算。
- 工时口径：整班 8 小时、半班 4 小时；加班单独记录。
- 规则细节见 `docs/BUSINESS-RULES.md`。

## 正式部署注意事项

- 关闭 `FDC_TEST_MODE`，不要配置 `FDC_TEST_LOGIN_SECRET`。
- 使用独立的强数据库口令，配置 TLS/反向代理。
- 未配置的钉钉/外部考勤集成不会发起外网或内网连接；页面会显示未启用。
- 数据库持久化与备份由部署方负责；迁移只执行一次，不要修改已应用的迁移文件。
- 健康检查：`GET /api/v1/health`。

## 测试

```bash
scripts/regression.sh          # API pytest + Web typecheck/build
scripts/regression.sh api
scripts/regression.sh web
scripts/regression.sh e2e      # 需 VERIFY_BASE_URL
```

## 许可证

**待确认。** 本项目代码、字体、图标等第三方素材的公开授权尚未核实。
在维护者确认并替换 `LICENSE` 之前，默认保留所有权利，不得对外再分发。

## 参考

- 架构：`docs/ARCHITECTURE.md`
- 业务规则：`docs/BUSINESS-RULES.md`
- 回归：`docs/REGRESSION.md`
