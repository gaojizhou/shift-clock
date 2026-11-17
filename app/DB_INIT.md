# 数据库初始化与迁移

本项目的 schema 与基础字典由 `app/api/migrations/*.sql` 定义，执行器为 `python -m app.migrate`。
`app/api/app/models.py` 是 SQLAlchemy ORM 模型，单元测试用 SQLite `create_all` 建表；生产环境以迁移为准。

## 规则

1. **目录**：`app/api/migrations/`，按文件名字典序执行，命名 `NNNN_说明.sql`。
2. **每个文件只执行一次**：执行记录落库 `schema_migration`（filename 唯一 + 内容 SHA256 + applied_at）。
3. **一个文件 = 一个事务**：中途失败整体回滚且不记账，修复后重跑从失败文件继续。
4. **历史不可改**：已执行文件被改动（校验和不一致）会直接报错；变更一律新增编号更大的迁移文件。
5. **并发安全**：PostgreSQL 事务级 advisory lock，多实例同时部署也只执行一次。

## 初始化

```bash
cd app/api
. .venv/bin/activate
python -m app.migrate            # 执行未执行的迁移（schema + 基础字典）
python -m app.migrate --status   # 查看状态，不执行
```

迁移只写入最小基础字典（角色、职位、地区示例、班组、设备类型、岗位字典、班次时间）。
**不会**写入员工、工作点、扫码或考勤等业务数据。

## 演示数据（可选，显式命令）

```bash
cd app/api
python -m app.seed              # 合成演示：2 个示例厂区、4 个工作点、10 名 DEMO 员工、三班时段
python -m app.seed --no-demo    # 只建表 + 基础字典
```

演示数据使用 `DEMO001` 等明显虚构工号，不含真实姓名、身份证、手机号或钉钉身份。
命令幂等：重复执行只补缺失，不覆盖已维护的数据。正式部署不要执行。

## Docker

- API / worker 启动时自动执行 `python -m app.migrate --skip-online-index`。
- 需要演示数据时单独执行：
  `docker compose -f app/docker-compose.yml run --rm api python -m app.seed`

## 清库重建（仅本地）

```bash
# 销毁数据卷 = 全新库，重启后迁移自动重放
docker compose -f app/docker-compose.yml down -v
docker compose -f app/docker-compose.yml up -d --build
```

> `TRUNCATE` 不会清 `schema_migration` 记录，不会触发重放；要全量重放必须清掉 schema 或迁移记录。
