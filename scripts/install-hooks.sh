#!/usr/bin/env bash
# 启用版本化 git hooks（scripts/git-hooks/），当前仅 pre-push 硬回归门禁。
# clone 后跑一次即可；scripts/regression.sh 每次运行也会自动确保已启用。

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

chmod +x scripts/git-hooks/* scripts/regression.sh 2>/dev/null || true
git config core.hooksPath scripts/git-hooks
echo "[hooks] core.hooksPath -> scripts/git-hooks（pre-push 硬回归已启用）"
