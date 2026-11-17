"""按序、每文件仅执行一次的 SQL 迁移执行器（替代 python -m app.seed）。

约定：
- 迁移文件放在 app/api/migrations/*.sql，按文件名字典序执行（建议 0001_xxx.sql 递增编号）。
- 每个文件可以是任意 SQL：建表+灌数据 / 插入行 / ALTER 加列……一个文件 = 一个事务。
- 执行记录写入 schema_migration（文件名唯一 + 内容 SHA256 + 时间）：
    * 已执行过的文件永不再执行（新环境一次性按序全量重放，向后兼容）。
    * 已执行过的文件内容被改动（校验和不一致）→ 直接报错拒绝继续，
      需要变更请新增一个编号更大的迁移文件，而不是改历史文件。
- Postgres 下用事务级 advisory lock 防止并发部署同时迁移。

用法：
    python -m app.migrate            # 执行所有未执行的迁移
    python -m app.migrate --status   # 只看状态不执行
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
ADVISORY_LOCK_KEY = 745301  # 任意固定值，标识"fdc 迁移"这把锁

_ONLINE_INDEX_DIRECTIVE = "fdc:postgresql-concurrent-index"
_CREATE_UNIQUE_INDEX_RE = re.compile(
    r"\bCREATE\s+UNIQUE\s+INDEX(?:\s+CONCURRENTLY)?(?:\s+IF\s+NOT\s+EXISTS)?\s+"
    r'(?P<name>"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)',
    flags=re.IGNORECASE,
)

_TRACK_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    id INTEGER PRIMARY KEY {autoinc},
    filename VARCHAR(255) NOT NULL UNIQUE,
    checksum VARCHAR(64) NOT NULL,
    applied_at TIMESTAMP{tz} NOT NULL DEFAULT {now}
)
"""


def _track_ddl(dialect: str) -> str:
    if dialect == "postgresql":
        return _TRACK_DDL.format(autoinc="GENERATED ALWAYS AS IDENTITY", tz="TZ", now="now()")
    # sqlite（单元测试）
    return _TRACK_DDL.format(autoinc="AUTOINCREMENT", tz="", now="CURRENT_TIMESTAMP")


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _split_statements(sql: str) -> list[str]:
    """朴素分句（按行尾分号切）：sqlite 驱动只接受单语句。
    Postgres 走 psycopg2 多语句路径，不经过这里。"""
    stmts: list[str] = []
    cur: list[str] = []
    for line in sql.splitlines():
        cur.append(line)
        bare = line.strip()
        if bare.endswith(";") and not bare.startswith("--"):
            stmts.append("\n".join(cur).strip().rstrip(";"))
            cur = []
    rest = "\n".join(cur).strip()
    if rest:
        stmts.append(rest.rstrip(";"))
    # 去掉纯注释/空白块
    return [
        s for s in stmts
        if any(ln.strip() and not ln.strip().startswith("--") for ln in s.splitlines())
    ]


def _exec_script(conn, dialect: str, sql: str) -> None:
    """执行多语句 SQL 脚本。psycopg2 原生支持多语句（整脚本同事务）；
    sqlite（单元测试）逐句执行——不可用 executescript，它会隐式提交破坏事务回滚。"""
    if dialect == "sqlite":
        for stmt in _split_statements(sql):
            conn.exec_driver_sql(stmt)
    else:
        conn.exec_driver_sql(sql)


def _rewrite_online_index_statement(stmt: str) -> tuple[str, str | None]:
    """Rewrite the actual DDL line, never a preceding SQL comment."""
    lines = stmt.splitlines()
    index_name: str | None = None
    for index, line in enumerate(lines):
        if line.lstrip().startswith("--"):
            continue
        match = _CREATE_UNIQUE_INDEX_RE.search(line)
        if not match:
            continue
        index_name = match.group("name").strip('"')
        lines[index] = re.sub(
            r"\bCREATE\s+UNIQUE\s+INDEX\b(?!\s+CONCURRENTLY)",
            "CREATE UNIQUE INDEX CONCURRENTLY",
            line,
            count=1,
            flags=re.IGNORECASE,
        )
        break
    return "\n".join(lines), index_name


def _postgres_index_valid(conn, index_name: str) -> bool | None:
    row = conn.execute(
        text(
            "SELECT i.indisvalid FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.oid = to_regclass(:index_name)"
        ),
        {"index_name": index_name},
    ).first()
    return bool(row[0]) if row is not None else None


def _run_postgres_online_index_migration(
    engine: Engine,
    *,
    filename: str,
    sql: str,
    digest: str,
) -> bool:
    """Run an idempotent index migration outside a transaction.

    PostgreSQL requires CREATE INDEX CONCURRENTLY to be top-level. A session-level
    advisory lock keeps parallel deployers serialized while every statement is
    autocommitted, so a crash can safely resume the IF NOT EXISTS script.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        acquired = bool(
            conn.execute(text(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_KEY})")).scalar()
        )
        if not acquired:
            raise RuntimeError("另一迁移正在执行，在线索引迁移拒绝无限等待")
        try:
            again = conn.execute(
                text("SELECT 1 FROM schema_migration WHERE filename = :fn"),
                {"fn": filename},
            ).first()
            if again:
                return False
            online_indexes: list[str] = []
            print(f"[migrate] 开始在线索引迁移 {filename}", flush=True)
            for stmt in _split_statements(sql):
                online_stmt, index_name = _rewrite_online_index_statement(stmt)
                if index_name:
                    validity = _postgres_index_valid(conn, index_name)
                    if validity is False:
                        # Interrupted CREATE INDEX CONCURRENTLY leaves an invalid relation;
                        # IF NOT EXISTS would silently accept it on the next deploy.
                        conn.exec_driver_sql(
                            f'DROP INDEX CONCURRENTLY IF EXISTS "{index_name}"'
                        )
                conn.exec_driver_sql(online_stmt)
                if index_name:
                    online_indexes.append(index_name)
            for index_name in online_indexes:
                if _postgres_index_valid(conn, index_name) is not True:
                    raise RuntimeError(
                        f"在线索引 {index_name} 创建后无效，迁移拒绝记账"
                    )
            conn.exec_driver_sql("SET search_path TO public")
            conn.execute(
                text("INSERT INTO schema_migration (filename, checksum) VALUES (:fn, :ck)"),
                {"fn": filename, "ck": digest},
            )
            return True
        finally:
            conn.execute(text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_KEY})"))


def run(
    engine: Engine | None = None,
    migrations_dir: Path | None = None,
    *,
    skip_online_indexes: bool = False,
) -> list[str]:
    """执行所有未执行的迁移，返回本次执行的文件名列表。"""
    if engine is None:
        from app.db import engine as app_engine

        engine = app_engine
    d = migrations_dir or MIGRATIONS_DIR
    # 跳过隐藏/AppleDouble 文件（如 macOS tar 残留的 ._0001_role.sql，非真实迁移、非 UTF-8）
    files = sorted(p for p in d.glob("*.sql") if p.is_file() and not p.name.startswith("."))
    dialect = engine.dialect.name

    with engine.begin() as conn:
        conn.execute(text(_track_ddl(dialect)))

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT filename, checksum FROM schema_migration")).all()
    applied = {r[0]: r[1] for r in rows}

    # 历史文件防篡改：已执行的文件内容必须保持不变
    for f in files:
        if f.name in applied:
            digest = _checksum(f.read_text(encoding="utf-8"))
            if digest != applied[f.name]:
                raise RuntimeError(
                    f"迁移文件 {f.name} 已执行过但内容被改动（校验和不一致）。"
                    "禁止修改历史迁移，请新增编号更大的迁移文件。"
                )

    executed: list[str] = []
    for f in files:
        if f.name in applied:
            continue
        sql = f.read_text(encoding="utf-8")
        digest = _checksum(sql)
        if dialect == "postgresql" and _ONLINE_INDEX_DIRECTIVE in sql:
            if skip_online_indexes:
                print(f"[migrate] 跳过启动路径中的在线索引迁移 {f.name}")
                # 保持迁移顺序：不得越过待执行的在线索引去应用后续文件。
                break
            if _run_postgres_online_index_migration(
                engine,
                filename=f.name,
                sql=sql,
                digest=digest,
            ):
                executed.append(f.name)
                print(f"[migrate] applied {f.name}（online index）")
            continue
        with engine.begin() as conn:  # 单文件一个事务：失败整体回滚且不记账
            if dialect == "postgresql":
                conn.execute(text(f"SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY})"))
                # 拿锁后复查（并发部署：另一进程可能刚执行完同名文件）
                again = conn.execute(
                    text("SELECT 1 FROM schema_migration WHERE filename = :fn"),
                    {"fn": f.name},
                ).first()
                if again:
                    continue
            _exec_script(conn, dialect, sql)
            if dialect == "postgresql":
                # pg_dump 脚本会 set_config('search_path','')，执行后重置以便记账
                conn.exec_driver_sql("SET search_path TO public")
            conn.execute(
                text("INSERT INTO schema_migration (filename, checksum) VALUES (:fn, :ck)"),
                {"fn": f.name, "ck": digest},
            )
        executed.append(f.name)
        print(f"[migrate] applied {f.name}")
    if not executed:
        print(f"[migrate] up-to-date（{len(files)} 个迁移均已执行）")
    return executed


def status(engine: Engine | None = None, migrations_dir: Path | None = None) -> None:
    if engine is None:
        from app.db import engine as app_engine

        engine = app_engine
    d = migrations_dir or MIGRATIONS_DIR
    files = sorted(p.name for p in d.glob("*.sql") if not p.name.startswith("."))
    try:
        with engine.connect() as conn:
            applied = {
                r[0] for r in conn.execute(text("SELECT filename FROM schema_migration")).all()
            }
    except Exception:
        applied = set()
    for name in files:
        print(f"  [{'x' if name in applied else ' '}] {name}")
    pending = [n for n in files if n not in applied]
    print(f"[migrate] 共 {len(files)} 个，待执行 {len(pending)} 个")


if __name__ == "__main__":
    if "--status" in sys.argv:
        status()
    else:
        run(skip_online_indexes="--skip-online-index" in sys.argv)
