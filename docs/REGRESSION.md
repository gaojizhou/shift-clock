# 回归流程设计

目标：让「提交 → 推送」天然带回归，人和 Agent 都不容易漏跑。
机制：pre-push 硬回归 hook（第一道门禁）+ GitHub Actions 回归 CI（第二道兜底）。

## 1. 回归分层

| 层级 | 内容 | 耗时 | 外部依赖 | 触发时机 |
|---|---|---|---|---|
| L1 API | `app/api` pytest（内存 SQLite） | ~15s | 无（不需要 PostgreSQL / 钉钉凭据） | pre-push 硬门禁 + CI |
| L2 WEB | `app/web` `pnpm -r typecheck` + `pnpm -r build` | ~20s | 无 | pre-push 硬门禁 + CI |
| L3 E2E | `app/e2e` Playwright（admin + h5） | 分钟级 | 需运行中的环境（`VERIFY_BASE_URL`） | 手动 / 发布前 |

L1/L2 是硬回归：密闭、秒级、无环境依赖，因此可以无条件卡推送。
L3 依赖起服务的环境，不进 pre-push，归入发布前验证。

## 2. 统一入口

```bash
scripts/regression.sh           # 全量硬回归（api + web）
scripts/regression.sh api       # 仅后端
scripts/regression.sh web       # 仅前端
scripts/regression.sh e2e       # Playwright（需 VERIFY_BASE_URL）
```

- 依赖自举：api 首跑自动建 `.venv` 装依赖；web 自动 `pnpm install --frozen-lockfile`。
- 退出码非 0 即失败；输出末尾有 PASS/FAIL 汇总和重跑指引。
- 每次运行会顺手启用 pre-push hook（幂等）。

## 3. pre-push 硬回归门禁

- hook 版本化在 `scripts/git-hooks/pre-push`，通过 `git config core.hooksPath scripts/git-hooks` 启用。
- 启用方式（任一即可，均幂等）：`scripts/install-hooks.sh`，或跑任意一次 `scripts/regression.sh`。
- 推送时按**本次实际推送的 diff** 圈定范围：
  - `app/api/**` → L1；`app/web/**` 及前端配置 → L2；
  - `app/` 下其他、`scripts/`、`.github/` → L1+L2 全跑（保守侧）；
  - 纯文档（`docs/`、`*.md`）→ 免测放行；
  - 新分支以 `origin/main`（回退 `origin/dev`）的 merge-base 为基线；删除远端分支放行。
- 任一层失败 → 推送被拦截，修红后重推。

### 逃生口（仅限事故热修）

`git push --no-verify` 可跳过 hook，仅限线上事故热修。跳过后 CI 兜底仍会跑；

## 4. CI 兜底

`.github/workflows/regression.yml`：所有分支 push + PR，`app/**`、`scripts/**` 变更时
并行跑 L1 + L2。

## 5. Agent 约定

1. 接触代码前先跑一次 `scripts/regression.sh`，确认基线绿（顺带启用 hook）；
2. 禁止用 `--no-verify` 绕过门禁（除非用户明确要求事故热修）；
3. 回归红灯不允许通过删测试/跳测试解决，需修复根因或与用户确认。

## 6. 维护规则

- **基线必须全绿**：不要长期保留红灯用例。
- 测试必须密闭（不依赖真实 PostgreSQL / 钉钉 / 日期），外部调用用 mock。
- 新增后端功能 → `app/api/tests/` 补 pytest；新增前端页面 → 保证 typecheck/build 覆盖，
  关键流程补 `app/e2e/` 用例。
- 新增回归层级时：同步修改 `scripts/regression.sh`、`scripts/git-hooks/pre-push`、`regression.yml`。
