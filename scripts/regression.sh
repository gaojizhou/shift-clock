#!/usr/bin/env bash
# 三班倒打卡（FDC）回归统一入口。
# 设计文档：docs/REGRESSION.md
#
# 用法：
#   scripts/regression.sh                 # 全量硬回归（api + web）
#   scripts/regression.sh api             # 仅后端（pytest，内存 SQLite，无外部依赖）
#   scripts/regression.sh web             # 仅前端（pnpm typecheck + build）
#   scripts/regression.sh e2e             # Playwright e2e（需 VERIFY_BASE_URL 指向运行中环境）
#   scripts/regression.sh api web         # 组合
#
# 退出码：0 = 全部通过；非 0 = 有失败（pre-push hook 据此拦截推送）。
# 依赖自举：api 首跑自动创建 .venv 并安装依赖；web 自动 pnpm install --frozen-lockfile。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 顺手确保 pre-push 硬回归 hook 已启用（幂等，防止 clone 后漏装）
if git rev-parse --git-dir >/dev/null 2>&1; then
  [ "$(git config core.hooksPath 2>/dev/null)" = "scripts/git-hooks" ] \
    || git config core.hooksPath scripts/git-hooks 2>/dev/null || true
fi

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
say()  { echo "${BOLD}[regression]${RESET} $*"; }
pass() { echo "${GREEN}[PASS]${RESET} $*"; }
fail() { echo "${RED}[FAIL]${RESET} $*"; }

FAILED=()

run_api() {
  say "── API 回归：pytest（app/api，内存 SQLite，无需 PG/钉钉凭据）"
  (
    cd app/api
    if [ ! -x .venv/bin/python ]; then
      say "首次运行：创建 .venv 并安装依赖（约 1-2 分钟）"
      python3 -m venv .venv || exit 1
      .venv/bin/pip install -q -e ".[dev]" || exit 1
    fi
    # 依赖漂移兜底：venv 存在但 pytest 缺失时补装
    .venv/bin/python -c "import pytest" 2>/dev/null || .venv/bin/pip install -q -e ".[dev]" || exit 1
    .venv/bin/python -m pytest -q
  )
  if [ $? -eq 0 ]; then pass "API pytest"; else fail "API pytest"; FAILED+=("api"); fi
}

run_web() {
  say "── WEB 回归：typecheck + build（app/web，admin + h5 + api-client）"
  (
    cd app/web
    command -v pnpm >/dev/null || { echo "pnpm 缺失，请先 corepack enable"; exit 1; }
    # node_modules 缺失或 lockfile 变更时按锁安装；已就绪时秒级返回
    pnpm install --frozen-lockfile --prefer-offline || exit 1
    pnpm -r typecheck || exit 1
    pnpm -r build || exit 1
  )
  if [ $? -eq 0 ]; then pass "WEB typecheck + build"; else fail "WEB typecheck + build"; FAILED+=("web"); fi
}

run_e2e() {
  say "── E2E 回归：Playwright（需 VERIFY_BASE_URL 指向运行中的环境）"
  if [ -z "${VERIFY_BASE_URL:-}" ]; then
    fail "E2E：缺少 VERIFY_BASE_URL（请先启动环境并导出该变量）"
    FAILED+=("e2e")
    return
  fi
  (
    cd app
    npx --no-install playwright test || exit 1
  )
  if [ $? -eq 0 ]; then pass "E2E playwright"; else fail "E2E playwright"; FAILED+=("e2e"); fi
}

SCOPES=("$@")
[ ${#SCOPES[@]} -eq 0 ] && SCOPES=(api web)

for s in "${SCOPES[@]}"; do
  case "$s" in
    api) run_api ;;
    web) run_web ;;
    e2e) run_e2e ;;
    all) run_api; run_web ;;
    *) echo "未知 scope: $s（可选 api / web / e2e / all）"; exit 2 ;;
  esac
done

echo
if [ ${#FAILED[@]} -gt 0 ]; then
  fail "回归未通过：${FAILED[*]}"
  echo "${YELLOW}修复后重跑：scripts/regression.sh ${FAILED[*]}${RESET}"
  exit 1
fi
pass "回归全部通过（${SCOPES[*]}）"
