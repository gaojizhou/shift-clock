# 三班倒打卡系统 · Agent 工作约定

## 回归门禁（必读）

本仓库有 pre-push 硬回归门禁，设计文档见 `docs/REGRESSION.md`。

- **会话开始接触代码前**：先跑 `scripts/regression.sh`，确认基线绿（同时自动启用
  `scripts/git-hooks/pre-push` hook；也可用 `scripts/install-hooks.sh` 单独启用）。
- **推送**：正常 `git push` 即可，hook 会按本次 diff 自动跑 api/web 回归，失败即拦截。
- **禁止** `git push --no-verify` 绕过门禁（除非用户明确要求事故热修）。
- 回归红灯不允许删测试/跳测试了事，修根因或与用户确认。

## 常用命令

```bash
scripts/regression.sh            # 全量硬回归（api pytest + web typecheck/build）
scripts/regression.sh api        # 仅后端（app/api，pytest，内存 SQLite，无外部依赖）
scripts/regression.sh web        # 仅前端（app/web，pnpm -r typecheck && build）
scripts/regression.sh e2e        # Playwright，需 VERIFY_BASE_URL
```

## 结构速览

- `app/api`：FastAPI + SQLAlchemy 后端，测试在 `app/api/tests`（密闭，勿引入真实 PG/钉钉依赖，
  外部调用用 mock）。
- `app/api/migrations`：按序执行的 PostgreSQL 迁移，schema 以此为准。
- `app/web`：pnpm workspace（`apps/admin`、`apps/h5`、`packages/api-client`）。
- `app/e2e`：Playwright 用例（发布前验证，不进 pre-push 门禁）。
- `docs/`：架构、业务规则、回归与部署说明。

## 数据与配置

- 不要把真实员工、考勤、薪资、连接凭据或客户部署地址写入仓库。
- 演示数据只用 `python -m app.seed` 生成的合成数据。
