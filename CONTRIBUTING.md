# 贡献指南

## 开发流程

1. 阅读 `AGENTS.md` 与 `docs/REGRESSION.md`，先跑一次 `scripts/regression.sh` 确认基线绿。
2. 从 `main` 拉分支开发。
3. 修改后本地跑相关测试：
   ```bash
   scripts/regression.sh api     # 后端
   scripts/regression.sh web     # 前端
   ```
4. 提交信息说明动机与影响；新增数据库变更必须新增迁移文件，不要改已应用的迁移。
5. 正常 `git push`，pre-push hook 会自动回归。

## 代码约定

- 后端：Python 3.11+，ruff line-length 100；测试保持密闭，不依赖真实 PostgreSQL/钉钉。
- 前端：TypeScript 严格模式；组件与类型改动需通过 `pnpm -r typecheck`。
- 数据库：schema 以 `app/api/migrations/*.sql` 为准，模型以 `app/api/app/models.py` 为准，两边同步。
- Python 依赖：`app/api/requirements.lock` 记录已验证的解析结果；升级依赖后同步更新。

## 不要提交

- 真实员工、考勤、薪资数据；真实姓名、身份证、手机号、钉钉 ID。
- `.env`、密钥、令牌、私钥、数据库文件、日志、构建产物。
- 客户部署地址、内网主机、私有镜像或客户专属配置。
- 客户资料、验收截图与内部实施文档。

## 演示数据

演示数据统一由 `python -m app.seed` 生成（工号 `DEMO` 前缀）。新增演示场景请扩展该命令，
不要在迁移或测试 fixture 中写入真实身份。

## 报告问题

- 安全问题请按 `SECURITY.md` 私下报告。
- 功能问题请提供复现步骤、期望行为与实际行为。
