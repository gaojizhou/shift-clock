"""SQL 迁移执行器：按序执行、每文件仅一次、防篡改、失败回滚不记账。"""

import pytest
from sqlalchemy import create_engine, text

from app.migrate import _rewrite_online_index_statement, run, status


@pytest.fixture
def env(tmp_path):
    db_file = tmp_path / "m.db"
    engine = create_engine(f"sqlite:///{db_file}")
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    return engine, mdir


def _q(engine, sql):
    with engine.connect() as c:
        return c.execute(text(sql)).all()


def test_runs_in_order_once(env):
    engine, mdir = env
    (mdir / "0001_create.sql").write_text(
        "CREATE TABLE t (a INTEGER);\nINSERT INTO t VALUES (1);", encoding="utf-8"
    )
    (mdir / "0002_add_rows.sql").write_text("INSERT INTO t VALUES (2);", encoding="utf-8")
    (mdir / "0003_alter.sql").write_text(
        "ALTER TABLE t ADD COLUMN b TEXT;\nUPDATE t SET b = 'x';", encoding="utf-8"
    )

    executed = run(engine, mdir)
    assert executed == ["0001_create.sql", "0002_add_rows.sql", "0003_alter.sql"]
    assert {r[0] for r in _q(engine, "SELECT a FROM t")} == {1, 2}
    assert {r[0] for r in _q(engine, "SELECT b FROM t")} == {"x"}

    # 再跑一遍：全部跳过，数据不重复
    assert run(engine, mdir) == []
    assert len(_q(engine, "SELECT a FROM t")) == 2

    # 新增一个迁移：只执行新的
    (mdir / "0004_more.sql").write_text("INSERT INTO t (a, b) VALUES (3, 'y');", encoding="utf-8")
    assert run(engine, mdir) == ["0004_more.sql"]
    assert len(_q(engine, "SELECT a FROM t")) == 3


def test_tampered_history_rejected(env):
    engine, mdir = env
    f = mdir / "0001_create.sql"
    f.write_text("CREATE TABLE t (a INTEGER);", encoding="utf-8")
    run(engine, mdir)
    f.write_text("CREATE TABLE t (a INTEGER); -- 偷偷改历史", encoding="utf-8")
    with pytest.raises(RuntimeError, match="校验和不一致"):
        run(engine, mdir)


def test_failure_rolls_back_and_not_recorded(env):
    engine, mdir = env
    (mdir / "0001_create.sql").write_text("CREATE TABLE t (a INTEGER);", encoding="utf-8")
    (mdir / "0002_bad.sql").write_text(
        "INSERT INTO t VALUES (1);\nINSERT INTO no_such_table VALUES (1);", encoding="utf-8"
    )
    with pytest.raises(Exception):
        run(engine, mdir)
    # 0001 已记账，0002 失败回滚且未记账（其中的第一条 INSERT 也不落库）
    applied = {r[0] for r in _q(engine, "SELECT filename FROM schema_migration")}
    assert applied == {"0001_create.sql"}
    assert _q(engine, "SELECT count(*) FROM t")[0][0] == 0
    # 修复后重跑成功
    (mdir / "0002_bad.sql").write_text("INSERT INTO t VALUES (1);", encoding="utf-8")
    assert run(engine, mdir) == ["0002_bad.sql"]


def test_status_smoke(env, capsys):
    engine, mdir = env
    (mdir / "0001_a.sql").write_text("CREATE TABLE t (a INTEGER);", encoding="utf-8")
    status(engine, mdir)
    run(engine, mdir)
    status(engine, mdir)
    out = capsys.readouterr().out
    assert "[ ] 0001_a.sql" in out and "[x] 0001_a.sql" in out


def test_online_index_rewrite_ignores_comment_and_returns_index_name():
    rewritten, index_name = _rewrite_online_index_statement(
        "-- CREATE UNIQUE INDEX is mentioned here\n"
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_event ON audit_log (event_id)"
    )
    assert "-- CREATE UNIQUE INDEX is mentioned here" in rewritten
    assert "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_event" in rewritten
    assert index_name == "uq_event"
