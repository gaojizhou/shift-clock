# Schema 迁移说明

本项目的数据库 schema 由 `app/api/migrations/*.sql` 定义，执行器为 `python -m app.migrate`。
不要依赖 `Base.metadata.create_all` 维护生产 schema：它只补建缺失的表，不会给已有表补列，
也不会执行数据/索引/视图变更。

## 本地初始化

```bash
cd app/api
. .venv/bin/activate
python -m app.migrate
```

## 新增迁移

1. 在 `app/api/migrations/` 新建 `NNNN_说明.sql`（编号取当前最大 +1）。
2. 涉及 ORM 字段时同步修改 `app/api/app/models.py`（测试用 SQLite `create_all`）。
3. 本地跑 `python -m app.migrate` + `pytest`。
4. 不要在已应用的旧迁移上改内容：执行器会因校验和不一致报错。

## 验证

```bash
pytest -q app/api/tests/test_migrate.py
```
