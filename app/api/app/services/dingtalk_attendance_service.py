"""钉钉考勤打卡同步 / 转换。

两步：
1) sync_range：从外部 MySQL 视图 view_external_att_data 拉工作点员工日 D 对应的
   D-1/D/D+1 原始打卡，幂等 upsert 进 Postgres（业务键 code+att_time+direct+src）。
2) transform_range：把 dingtalk_attendance_raw 映射成 laoqin_punch（劳勤工作台对账数据源），
   去重写入，再对涉及日期跑 run_recon_for_date → 数据出现在「劳勤工作台」。

日常入口是 pull_range（后台任务壳 start_pull）：只拉指定范围内有 active/待审工作点记录、
且未被软删除的员工，再按天转换和对账，避免存储与本系统无关的集团考勤。
sync_range 仅拉原始打卡；单人重拉可传 codes 进一步限定员工。

连接参数与视图名见 app.config.settings.attendance_mysql_* / attendance_result_view。
"""

from __future__ import annotations

from app.services.sync_log_service import logged_start, logged_sync, add_counts, mark

import json
import pickle
import tempfile
import hashlib
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.domain.timezone import SHANGHAI_TIMEZONE, as_shanghai_datetime
from app.models import (
    AttendanceRecord,
    AuditLog,
    DingtalkAttendanceRaw,
    DingtalkSyncState,
    Employee,
    ExternalAttendanceResultRaw,
    LaoqinPunch,
    MonthlyExportLock,
    ReconResult,
)
from app.services.recon_service import load_recon_ctx, run_recon_for_date

logger = logging.getLogger(__name__)

# 劳勤转换后台任务：进度状态行 kind（复用 dingtalk_sync_state 表）+ 防并发锁
TRANSFORM_KIND = "attendance_transform"
ATT_RESULT_KIND = "attendance_result_pull"
TASK_LEASE_KIND = "attendance_task_lease"
_transform_lock = threading.Lock()
STALE_RUNNING_MINUTES = 30
ELIGIBLE_ATTENDANCE_STATUSES = ("active", "待审")
SOURCE_TIMEZONE = SHANGHAI_TIMEZONE

# 视图列（顺序与 SELECT 对应）
_VIEW_COLS = [
    "tenant_code", "code", "name", "org", "layer",
    "att_time", "direct", "state", "src", "detail", "create_time", "modify_time",
]

_ATT_RESULT_COLS = [
    "tenant_code", "dept_name", "layer_name", "hr_status", "hr_type",
    "user_code", "user_name", "kq_date_show", "kq_date_title", "kq_date", "kqqs",
    "status_name", "lx_type", "actual_on", "actual_off", "quota", "cqgs", "kggs",
    "cd2", "zt2", "ot1", "ot2", "ot3", "leave_01", "leave_02", "leave_03",
    "leave_04", "leave_05", "leave_06", "leave_07", "leave_09", "leave_10",
    "leave_12", "leave_13",
    "ext1", "ext2", "business_01", "business_02",
]
# 上游：01=年休假、02=排休假、03=事假、04=全薪病假（以上单位为小时）；
# 05=病假、06=计生假、07=婚假、09=产假、10=陪产假、12=丧假、13=工伤假。
# 除 06（计生假）单位待确认外，其余已确认均为小时；同步层全部按源值镜像。
# business_02=出差小时数。

# 补卡来源标识：src=工作流 即审批补卡（detail 为 {"reason":N,...}、address 空）。
_MAKEUP_SRC = "工作流"
# 钉钉补卡 reason 码 → 可读原因（未知码兜底「工作流补卡(码N)」）。
_MAKEUP_REASON_MAP = {
    0: "工作流补卡",
    1: "出差",
    2: "外出",
    3: "其他",
}


class TaskLeaseLost(RuntimeError):
    """任务数据库租约已失效或被其他进程接管，当前任务必须立即停止。

    与普通拉取/转换失败区分开：不能被包装成「重跑即可续传」的误导文案，
    也不允许以旧 owner 身份继续写数据。"""


def _makeup_reason(src: str | None, detail: str | None) -> str | None:
    """src=工作流 时，从 detail.reason 映射可读补卡原因；非工作流或无法解析返回 None。"""
    if src != _MAKEUP_SRC:
        return None
    code = None
    if detail:
        try:
            code = json.loads(detail).get("reason")
        except (ValueError, TypeError):
            code = None
    if code is None:
        return "工作流补卡"
    return _MAKEUP_REASON_MAP.get(code, f"工作流补卡(码{code})")


def _validate_date(s: str) -> str:
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"非法日期：{s!r}，应为 YYYY-MM-DD")
    return s


def _date_list(date_from: str, date_to: str) -> list[str]:
    d0 = datetime.strptime(date_from, "%Y-%m-%d").date()
    d1 = datetime.strptime(date_to, "%Y-%m-%d").date()
    if d1 < d0:
        d0, d1 = d1, d0
    out, cur = [], d0
    while cur <= d1:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _mysql_connect():
    """连接外部钉钉考勤源库（只读账号）。"""
    import pymysql

    if not settings.attendance_mysql_password:
        raise RuntimeError(
            "未配置钉钉考勤源库密码：请在服务器 .env 设置 ATTENDANCE_MYSQL_PASSWORD"
        )
    return pymysql.connect(
        host=settings.attendance_mysql_host,
        port=settings.attendance_mysql_port,
        user=settings.attendance_mysql_user,
        password=settings.attendance_mysql_password,
        database=settings.attendance_mysql_db,
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=120,
    )


def _source_error(e: Exception) -> RuntimeError:
    return RuntimeError(
        f"无法连接钉钉考勤源库（仅外部系统可同步）：{e}。"
        "外网机器请在内网用「导出 CSV」，再到本机「导入 CSV」搬运数据。"
    )


def _fetch_rows(conn, date_from: str, date_to: str, codes: list[str] | None = None) -> tuple:
    """按 att_time 范围 SELECT 视图行（单条查询，范围大小由调用方控制）。"""
    view = settings.attendance_mysql_view
    sql = (
        "SELECT " + ",".join("`%s`" % c for c in _VIEW_COLS) +
        f" FROM `{view}` WHERE att_time >= %s AND att_time <= %s"
    )
    params: list = [f"{date_from} 00:00:00", f"{date_to} 23:59:59.999"]
    codes = [c for c in (codes or []) if c]
    if codes:
        sql += " AND `code` IN (%s)" % ",".join(["%s"] * len(codes))
        params.extend(codes)
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return rows


def _date_key(v) -> str:
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    s = str(v or "").strip()
    return s[:10]


def _raw_str(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return _source_datetime(v).strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


def _source_datetime(v: datetime | None) -> datetime | None:
    """把 MySQL DATETIME 明确解释为上海本地时间。

    PyMySQL 对 DATETIME 返回 naive datetime。若直接写入 PostgreSQL timestamptz，
    PostgreSQL 会按会话时区解释它，导致同一份源数据在 UTC/上海环境产生不同结果。
    同步边界在这里补齐时区，使入库时刻不再依赖数据库默认时区。
    """
    if v is None:
        return None
    return as_shanghai_datetime(v)


def _fetch_result_rows(conn, date_from: str, date_to: str, codes: list[str] | None = None) -> tuple:
    """按 kq_date 范围 SELECT 外部考勤结果视图行。视图名由 ATTENDANCE_RESULT_VIEW 配置。"""
    view = settings.attendance_result_view
    if not view:
        raise RuntimeError(
            "未配置外部考勤结果视图：请设置 ATTENDANCE_RESULT_VIEW，或关闭结果拉取功能"
        )
    sql = (
        "SELECT " + ",".join("`%s`" % c for c in _ATT_RESULT_COLS) +
        f" FROM `{view}` WHERE kq_date >= %s AND kq_date <= %s"
    )
    params: list = [date_from, date_to]
    codes = [c for c in (codes or []) if c]
    if codes:
        sql += " AND `user_code` IN (%s)" % ",".join(["%s"] * len(codes))
        params.extend(codes)
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return rows


def sync_range(
    db: Session,
    date_from: str,
    date_to: str,
    codes: list[str] | None = None,
    progress_cb=None,
) -> dict:
    """只为范围内有有效/待审工作点记录的员工拉取打卡。

    工作点记录归属日为 D 时，证据卡拉 D-1/D/D+1，以覆盖跨夜班和班次前后 12h。
    codes 非空用于单人重拉：仍须先命中工作点记录，且不会清理其他员工的数据。
    """
    _validate_date(date_from)
    _validate_date(date_to)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    requested_codes = ({c for c in codes if c} if codes is not None else None)
    eligible = _eligible_codes_by_date(
        db, date_from, date_to, requested_codes=requested_codes
    )
    punch_codes = _punch_code_scope(
        db, date_from, date_to, requested_codes=requested_codes
    )
    target_codes = sorted({c for rows in eligible.values() for c in rows})
    source_dates = sorted(punch_codes)
    if not any(punch_codes.values()):
        for i, source_date in enumerate(source_dates, start=1):
            if progress_cb:
                progress_cb(i, len(source_dates), source_date)
        removed = _prune_raw_scope(
            db, punch_codes, requested_codes=requested_codes
        )
        return {
            "dateFrom": date_from,
            "dateTo": date_to,
            "employeeCount": 0,
            "eligibleDates": 0,
            "eligibleEmployeeDays": 0,
            "sourceDates": len(source_dates),
            "fetched": 0,
            "deduped": 0,
            "upserted": 0,
            "removed": removed,
        }
    try:
        conn = _mysql_connect()
    except RuntimeError:
        raise  # 缺密码等已是明确中文提示
    except Exception as e:  # noqa: BLE001 - pymysql 连接/读取失败（外网无法直连内网源库）
        raise _source_error(e)

    agg = {"fetched": 0, "deduped": 0, "upserted": 0}
    try:
        for i, source_date in enumerate(source_dates, start=1):
            day_codes = sorted(punch_codes[source_date])
            try:
                s = (
                    _sync_one_day(conn, db, source_date, day_codes)
                    if day_codes
                    else {"fetched": 0, "deduped": 0, "upserted": 0}
                )
            except Exception as e:  # noqa: BLE001
                db.rollback()
                raise RuntimeError(
                    f"{source_date} 拉取原始考勤失败：{e}（此前完成的日期已入库，修复后重跑同范围即续传）"
                )
            for k in agg:
                agg[k] += s[k]
            if progress_cb:
                progress_cb(i, len(source_dates), source_date)
    finally:
        conn.close()
    removed = _prune_raw_scope(
        db,
        punch_codes,
        requested_codes=requested_codes,
    )
    return {
        "dateFrom": date_from,
        "dateTo": date_to,
        "employeeCount": len(target_codes),
        "eligibleDates": len(eligible),
        "eligibleEmployeeDays": sum(len(v) for v in eligible.values()),
        "sourceDates": len(source_dates),
        "removed": removed,
        **agg,
    }


def _upsert_raw_rows(db: Session, rows) -> dict:
    """业务键(code, att_time, direct, src)去重后分批 upsert 进 dingtalk_attendance_raw 并提交。"""
    fetched = len(rows)
    add_counts(rawFetched=fetched)

    def flush(batch: list[dict]) -> int:
        if not batch:
            return 0
        stmt = pg_insert(DingtalkAttendanceRaw).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["code", "att_time", "direct", "src"],
            set_={
                "tenant_code": stmt.excluded.tenant_code,
                "name": stmt.excluded.name,
                "org": stmt.excluded.org,
                "layer": stmt.excluded.layer,
                "state": stmt.excluded.state,
                "detail": stmt.excluded.detail,
                "natual_date": stmt.excluded.natual_date,
                "src_create_time": stmt.excluded.src_create_time,
                "src_modify_time": stmt.excluded.src_modify_time,
                "synced_at": datetime.now(timezone.utc),
            },
        )
        db.execute(stmt)
        return len(batch)

    # 源视图存在重复行（同业务键多条）→ 先按业务键(code, att_time, direct, src)去重，
    # 否则同一批 INSERT ... ON CONFLICT 会命中同一行两次而报 CardinalityViolation。
    by_key: dict[tuple, dict] = {}
    for r in rows:
        rec = dict(zip(_VIEW_COLS, r))
        att = _source_datetime(rec["att_time"])
        if att is None:
            continue
        direct = rec.get("direct") or ""
        src = rec.get("src") or ""
        by_key[(rec["code"], att, direct, src)] = {
            "tenant_code": rec.get("tenant_code"),
            "code": rec["code"],
            "name": rec.get("name"),
            "org": rec.get("org"),
            "layer": rec.get("layer"),
            "att_time": att,
            "direct": direct,
            "state": rec.get("state"),
            "src": src,
            "detail": rec.get("detail"),
            "natual_date": att.strftime("%Y-%m-%d"),
            "src_create_time": _source_datetime(rec.get("create_time")),
            "src_modify_time": _source_datetime(rec.get("modify_time")),
        }

    values = list(by_key.values())
    upserted = 0
    for i in range(0, len(values), 500):
        upserted += flush(values[i:i + 500])
    db.commit()
    add_counts(rawUpserted=upserted)
    return {"fetched": fetched, "deduped": len(values), "upserted": upserted}


def _sync_one_day(conn, db: Session, date: str, codes: list[str] | None = None) -> dict:
    """拉取并入库单日打卡（复用调用方的 MySQL 连接）。"""
    return _upsert_raw_rows(db, _fetch_rows(conn, date, date, codes))


def _sync_raw_for_code(conn, db: Session, date_from: str, date_to: str, code: str) -> dict:
    """拉取并入库单个员工指定范围的原始打卡（复用调用方的 MySQL 连接）。"""
    return _upsert_raw_rows(db, _fetch_rows(conn, date_from, date_to, [code]))


def _upsert_result_rows(db: Session, rows, *, commit=True) -> dict:
    """按(user_code, kq_date)幂等 upsert 劳勤日结果视图行。"""
    fetched = len(rows)
    if commit:
        add_counts(resultFetched=fetched)
    by_key: dict[tuple[str, str], dict] = {}
    for r in rows:
        rec = dict(zip(_ATT_RESULT_COLS, r))
        user_code = (rec.get("user_code") or "").strip()
        kq_date = _date_key(rec.get("kq_date"))
        if not user_code or not kq_date:
            continue
        item = {c: _raw_str(rec.get(c)) for c in _ATT_RESULT_COLS}
        item["user_code"] = user_code
        item["kq_date"] = kq_date
        item["source_withdrawn_at"] = None
        by_key[(user_code, kq_date)] = item

    values = list(by_key.values())
    upserted = 0
    for i in range(0, len(values), 500):
        batch = values[i:i + 500]
        if not batch:
            continue
        stmt = pg_insert(ExternalAttendanceResultRaw).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_code", "kq_date"],
            set_={c: getattr(stmt.excluded, c) for c in _ATT_RESULT_COLS if c not in ("user_code", "kq_date")} | {
                "synced_at": datetime.now(timezone.utc),
                "source_withdrawn_at": None,
            },
        )
        db.execute(stmt)
        upserted += len(batch)
    if commit:
        db.commit()
        add_counts(resultUpserted=upserted)
    else:
        db.flush()
    return {"fetched": fetched, "deduped": len(values), "upserted": upserted}


def _active_employee_codes(db: Session) -> list[str]:
    return [
        c
        for c in db.scalars(
            select(Employee.emp_id)
            .where(Employee.deleted.is_(False))
            .order_by(Employee.emp_id)
        ).all()
        if c
    ]


def _eligible_codes_by_date(
    db: Session,
    date_from: str,
    date_to: str,
    *,
    requested_codes: set[str] | None = None,
) -> dict[str, set[str]]:
    """工作点员工日白名单：有效或待审记录，且员工未软删除。"""
    stmt = (
        select(AttendanceRecord.natual_date, AttendanceRecord.emp_id)
        .join(Employee, Employee.emp_id == AttendanceRecord.emp_id)
        .where(
            AttendanceRecord.natual_date >= date_from,
            AttendanceRecord.natual_date <= date_to,
            AttendanceRecord.status.in_(ELIGIBLE_ATTENDANCE_STATUSES),
            Employee.deleted.is_(False),
        )
        .distinct()
        .order_by(AttendanceRecord.natual_date, AttendanceRecord.emp_id)
    )
    if requested_codes is not None:
        stmt = stmt.where(AttendanceRecord.emp_id.in_(requested_codes))
    out: dict[str, set[str]] = {}
    for date, emp_id in db.execute(stmt):
        out.setdefault(date, set()).add(emp_id)
    return out


def _eligible_result_codes_by_date(
    db: Session,
    date_from: str,
    date_to: str,
    *,
    requested_codes: set[str] | None = None,
) -> dict[str, set[str]]:
    """考勤结果月白名单：当月有一次有效/待审工作点记录，即同步该员工整月结果。

    请求跨自然月时，各月独立计算员工集合；即使请求只覆盖月内部分日期，资格仍按
    该自然月完整范围判断，避免请假日因当天没有工作点记录而被排除。
    """
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    month_start = start.replace(day=1)
    end_month_start = end.replace(day=1)
    next_month = (
        end_month_start.replace(day=28) + timedelta(days=4)
    ).replace(day=1)
    month_end = next_month - timedelta(days=1)
    stmt = (
        select(AttendanceRecord.natual_date, AttendanceRecord.emp_id)
        .join(Employee, Employee.emp_id == AttendanceRecord.emp_id)
        .where(
            AttendanceRecord.natual_date >= month_start.isoformat(),
            AttendanceRecord.natual_date <= month_end.isoformat(),
            AttendanceRecord.status.in_(ELIGIBLE_ATTENDANCE_STATUSES),
            Employee.deleted.is_(False),
        )
        .distinct()
    )
    if requested_codes is not None:
        stmt = stmt.where(AttendanceRecord.emp_id.in_(requested_codes))
    codes_by_month: dict[str, set[str]] = {}
    for date, emp_id in db.execute(stmt):
        codes_by_month.setdefault(date[:7], set()).add(emp_id)
    return {
        date: set(codes_by_month[date[:7]])
        for date in _date_list(date_from, date_to)
        if codes_by_month.get(date[:7])
    }


def _punch_codes_by_source_date(
    eligible: dict[str, set[str]],
) -> dict[str, set[str]]:
    """工作点归属日 D 的员工需要 D-1/D/D+1 三个自然日的打卡。"""
    out: dict[str, set[str]] = {}
    for date, codes in eligible.items():
        d = datetime.strptime(date, "%Y-%m-%d").date()
        for offset in (-1, 0, 1):
            out.setdefault((d + timedelta(days=offset)).isoformat(), set()).update(codes)
    return out


def _punch_code_scope(
    db: Session,
    date_from: str,
    date_to: str,
    *,
    requested_codes: set[str] | None = None,
) -> dict[str, set[str]]:
    """返回本次要替换的打卡自然日及各日允许工号。

    边界自然日可能同时服务范围外相邻的工作点员工日，因此资格查询额外扩一日，
    再把结果裁剪到 [date_from-1, date_to+1]，避免同步一个月时误删相邻月证据。
    """
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    eligible = _eligible_codes_by_date(
        db,
        (start - timedelta(days=1)).isoformat(),
        (end + timedelta(days=1)).isoformat(),
        requested_codes=requested_codes,
    )
    expanded = _punch_codes_by_source_date(eligible)
    source_dates = _date_list(
        (start - timedelta(days=1)).isoformat(),
        (end + timedelta(days=1)).isoformat(),
    )
    return {date: expanded.get(date, set()) for date in source_dates}


def _delete_outside_codes(
    db: Session,
    model,
    *,
    date_attr,
    code_attr,
    codes_by_date: dict[str, set[str]],
    requested_codes: set[str] | None,
    commit: bool = True,
) -> int:
    """按自然日结除白名单外数据；单人重拉只触碰指定工号。"""
    removed = 0
    for date, allowed in codes_by_date.items():
        stmt = delete(model).where(date_attr == date)
        if model is ExternalAttendanceResultRaw:
            stmt = stmt.where(model.source_withdrawn_at.is_(None))
        if requested_codes is not None:
            stmt = stmt.where(code_attr.in_(requested_codes))
        if allowed:
            stmt = stmt.where(code_attr.notin_(allowed))
        result = db.execute(stmt)
        removed += int(result.rowcount or 0)
    if commit:
        db.commit()
    else:
        db.flush()
    return removed


def _prune_raw_scope(
    db: Session,
    codes_by_date: dict[str, set[str]],
    *,
    requested_codes: set[str] | None,
) -> int:
    return _delete_outside_codes(
        db,
        DingtalkAttendanceRaw,
        date_attr=DingtalkAttendanceRaw.natual_date,
        code_attr=DingtalkAttendanceRaw.code,
        codes_by_date=codes_by_date,
        requested_codes=requested_codes,
    )


def _validate_result_snapshot(rows, date, codes):
    # 不允许异常形状、错日期或越界工号被当成一次完整的空结果。
    seen = set()
    for row in rows:
        if len(row) != len(_ATT_RESULT_COLS):
            raise ValueError("源考勤字段数量不完整")
        item = dict(zip(_ATT_RESULT_COLS, row))
        code = (item.get("user_code") or "").strip()
        if code not in codes or _date_key(item.get("kq_date")) != date:
            raise ValueError("源考勤返回了范围外记录")
        if code in seen:
            raise ValueError("源考勤同一员工同一天存在重复记录，拒绝覆盖")
        seen.add(code)


def _apply_result_snapshot(db, rows, date, codes):
    model = ExternalAttendanceResultRaw
    existing = {r.user_code: r for r in db.scalars(select(model).where(
        model.kq_date == date, model.user_code.in_(codes)
    ))}
    returned = {str(row[_ATT_RESULT_COLS.index("user_code")]).strip() for row in rows}
    inserted = len(returned - existing.keys())
    restored = sum(existing[c].source_withdrawn_at is not None for c in returned & existing.keys())
    withdrawn = 0
    now = datetime.now(timezone.utc)
    for code, record in existing.items():
        if code not in returned and record.source_withdrawn_at is None:
            record.source_withdrawn_at = now
            withdrawn += 1
    stats = _upsert_result_rows(db, rows, commit=False)
    # Core upsert 不更新 identity map；后面的对账必须读取本次快照。
    db.flush()
    db.expire_all()
    return {**stats, "inserted": inserted, "updated": len(returned & existing.keys()),
            "withdrawn": withdrawn, "restored": restored}


def _finish_result_day(db, date, allowed_codes, requested_codes, *, commit=True):
    """Publish new result hours to persisted review queues in the same write flow."""
    removed = _delete_outside_codes(
        db, ExternalAttendanceResultRaw,
        date_attr=ExternalAttendanceResultRaw.kq_date,
        code_attr=ExternalAttendanceResultRaw.user_code,
        codes_by_date={date: allowed_codes}, requested_codes=requested_codes, commit=commit,
    )
    # Include deleted source rows so stale conclusions are also refreshed.
    # Reconciliation itself preserves manual decisions and monthly locks.
    run_recon_for_date(db, date, employee_ids=requested_codes, commit=commit)
    return removed


def pull_attendance_result_range(
    db: Session,
    date_from: str,
    date_to: str,
    progress_cb=None,
    codes: list[str] | None = None,
) -> dict:
    """按工作点员工月白名单拉取 view_external_att_result 日结果。"""
    _validate_date(date_from)
    _validate_date(date_to)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    dates = _date_list(date_from, date_to)
    requested_codes = ({c for c in codes if c} if codes is not None else None)
    eligible = _eligible_result_codes_by_date(
        db, date_from, date_to, requested_codes=requested_codes
    )
    target_codes = sorted({c for rows in eligible.values() for c in rows})
    if not target_codes:
        removed = 0
        for i, date in enumerate(dates, start=1):
            removed += _finish_result_day(db, date, set(), requested_codes)
            if progress_cb:
                progress_cb(i, len(dates), date)
        return {
            "dateFrom": date_from,
            "dateTo": date_to,
            "dates": dates,
            "employeeCount": 0,
            "eligibleDates": 0,
            "eligibleEmployeeDays": 0,
            "synced": {
                "fetched": 0,
                "deduped": 0,
                "upserted": 0,
                "removed": removed,
            },
        }
    try:
        conn = _mysql_connect()
    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _source_error(e)

    agg = {"fetched": 0, "deduped": 0, "upserted": 0, "inserted": 0, "updated": 0, "withdrawn": 0, "restored": 0}
    removed = 0
    # 先完整读取本次范围，临时文件避免整月结果常驻内存。任何读取失败都不改本地。
    with tempfile.TemporaryFile() as snapshot:
        try:
            for read_index, date in enumerate(dates, start=1):
                day_codes = sorted(eligible.get(date, set()))
                rows = _fetch_result_rows(conn, date, date, day_codes) if day_codes else []
                add_counts(resultFetched=len(rows))
                _validate_result_snapshot(rows, date, set(day_codes))
                pickle.dump((date, day_codes, rows), snapshot)
                if progress_cb:
                    progress_cb(read_index, len(dates) + 1, date)
        except Exception as e:
            db.rollback()
            raise RuntimeError(f"{date} 拉取考勤结果失败：{e}（本次范围未更新，原数据已保留）") from e
        finally:
            conn.close()
        snapshot.seek(0)
        try:
            for i, date in enumerate(dates, start=1):
                date, day_codes, rows = pickle.load(snapshot)
                stats = _apply_result_snapshot(db, rows, date, set(day_codes))
                for key in agg:
                    agg[key] += stats.get(key, 0)
                removed += _finish_result_day(db, date, set(day_codes), requested_codes, commit=False)
            db.commit()
        except Exception as e:
            db.rollback()
            raise RuntimeError(f"考勤结果保存失败：{e}（本次范围已回滚，原数据已保留）") from e
    add_counts(resultUpserted=agg["upserted"],
               resultInserted=agg["inserted"], resultUpdated=agg["updated"],
               resultWithdrawn=agg["withdrawn"], resultRestored=agg["restored"])
    if progress_cb:
        progress_cb(len(dates) + 1, len(dates) + 1, dates[-1])
    return {
        "dateFrom": date_from,
        "dateTo": date_to,
        "dates": dates,
        "employeeCount": len(target_codes),
        "eligibleDates": len(eligible),
        "eligibleEmployeeDays": sum(len(v) for v in eligible.values()),
        "synced": {**agg, "removed": removed},
    }


def pull_range(
    db: Session,
    date_from: str,
    date_to: str,
    progress_cb=None,
    codes: list[str] | None = None,
) -> dict:
    """按工作点员工日拉原始打卡，再转换劳勤并重算对账。"""
    _validate_date(date_from)
    _validate_date(date_to)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    dates = _date_list(date_from, date_to)

    sync_res = sync_range(db, date_from, date_to, codes=codes, progress_cb=progress_cb)
    transform_cb = None
    if progress_cb is not None:
        # 转劳勤+逐日对账阶段同样要逐日心跳/推进度，否则整月范围一旦该阶段
        # 超过 30 分钟心跳阈值，轮询会把运行中的任务误判为中断、租约可被抢占。
        # 进度在拉取完成数上累加，保证全程单调不回退。
        sync_done = sync_res.get("sourceDates") or len(dates)

        def transform_cb(done: int, total: int, date: str) -> None:
            progress_cb(sync_done + done, sync_done + total, date)

    try:
        tf_res = transform_range(
            db, date_from, date_to, codes=codes, progress_cb=transform_cb
        )
    except TaskLeaseLost:
        # 租约失效必须原样上抛：任务已被接管，不能再以「重跑续传」的口径包装，
        # 也不允许继续写数据。
        raise
    except Exception as e:  # noqa: BLE001
        db.rollback()
        raise RuntimeError(
            f"{date_from}~{date_to} 转劳勤失败：{e}（原始考勤已入库，修复后重跑转换即可续传）"
        )

    matched_stmt = select(func.count()).select_from(ReconResult).where(
        ReconResult.date.in_(dates)
    )
    if codes is not None:
        matched_stmt = matched_stmt.where(ReconResult.emp_id.in_(codes))
    matched = db.scalar(matched_stmt) or 0
    return {
        "dateFrom": date_from, "dateTo": date_to, "dates": dates,
        "synced": {
            "fetched": sync_res["fetched"],
            "deduped": sync_res["deduped"],
            "upserted": sync_res["upserted"],
            "employeeCount": sync_res.get("employeeCount", 0),
            "eligibleDates": sync_res.get("eligibleDates", 0),
            "eligibleEmployeeDays": sync_res.get("eligibleEmployeeDays", 0),
            "sourceDates": sync_res.get("sourceDates", 0),
        },
        "transformed": {**tf_res, "matched": matched},
    }


def _transform_punch_day(db: Session, date: str, codes: set[str]) -> dict:
    """把一个打卡自然日内白名单员工的 raw 幂等转换为 laoqin_punch。"""
    if not codes:
        return {
            "rawCount": 0,
            "inserted": 0,
            "skipped": 0,
            "skippedNoUser": 0,
            "removedNoUser": 0,
        }
    raws = db.scalars(
        select(DingtalkAttendanceRaw)
        .where(
            DingtalkAttendanceRaw.natual_date == date,
            DingtalkAttendanceRaw.code.in_(codes),
        )
        .order_by(DingtalkAttendanceRaw.att_time)
    ).all()

    # 当日已存在的业务键（emp_id, punch_time, inout_type）→ 幂等跳过
    existing = set(
        db.execute(
            select(LaoqinPunch.emp_id, LaoqinPunch.punch_time, LaoqinPunch.inout_type)
            .where(
                LaoqinPunch.natual_date == date,
                LaoqinPunch.emp_id.in_(codes),
            )
        ).all()
    )

    inserted = skipped = 0
    for r in raws:
        key = (r.code, r.att_time, r.direct or "自动判断")
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        db.add(LaoqinPunch(
            emp_id=r.code,
            org_name=r.org,
            punch_time=r.att_time,
            inout_type=r.direct or "自动判断",
            record_source=r.src or "钉钉打卡",
            makeup_reason=_makeup_reason(r.src, r.detail),
            natual_date=r.natual_date,
            raw_name=r.name,
        ))
        inserted += 1
    db.commit()
    return {
        "rawCount": len(raws), "inserted": inserted, "skipped": skipped,
        "skippedNoUser": 0, "removedNoUser": 0,
    }


def transform_range(
    db: Session,
    date_from: str,
    date_to: str,
    progress_cb=None,
    codes: list[str] | None = None,
) -> dict:
    """把 dingtalk_attendance_raw 在范围内的打卡映射成 laoqin_punch（去重），再跑对账。

    按天拆分、逐日提交（可续传、不长事务锁表）。progress_cb(done, total, date) 可选，用于推进度。
    """
    _validate_date(date_from)
    _validate_date(date_to)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    dates = _date_list(date_from, date_to)

    requested_codes = ({c for c in codes if c} if codes is not None else None)
    eligible = _eligible_codes_by_date(
        db, date_from, date_to, requested_codes=requested_codes
    )
    punch_codes = _punch_code_scope(
        db, date_from, date_to, requested_codes=requested_codes
    )
    ctx = load_recon_ctx(db)

    agg = {"rawCount": 0, "inserted": 0, "skipped": 0, "skippedNoUser": 0, "removedNoUser": 0}
    removed = _delete_outside_codes(
        db,
        LaoqinPunch,
        date_attr=LaoqinPunch.natual_date,
        code_attr=LaoqinPunch.emp_id,
        codes_by_date=punch_codes,
        requested_codes=requested_codes,
    )
    agg["removedNoUser"] += removed
    punch_dates = sorted(punch_codes)
    # 进度分两段：打卡转换（源自然日，可能比归属日多 ±1 天）→ 逐日对账。
    # 打卡转换循环是耗时大头，必须逐日回调 progress_cb 完成心跳续约，
    # 否则整月范围一旦该段超过 30 分钟阈值，租约会被其他 worker 抢占、状态被误判中断。
    total_units = len(punch_dates) + len(dates)
    for i, source_date in enumerate(punch_dates, start=1):
        day = _transform_punch_day(db, source_date, punch_codes[source_date])
        add_counts(punchInserted=day["inserted"], punchSkipped=day["skipped"])
        for k in agg:
            agg[k] += day[k]
        if progress_cb:
            progress_cb(i, total_units, source_date)
    for i, d in enumerate(dates, start=1):
        # 只为工作点员工日生成对账；无资格日期也执行一次以清理历史孤儿。
        run_recon_for_date(db, d, ctx=ctx, employee_ids=requested_codes)
        if progress_cb:
            progress_cb(len(punch_dates) + i, total_units, d)

    # 统计：涉及人数、匹配花名册人数、对账产出条数（=进入劳勤工作台的记录数）
    total_stmt = (
        select(func.count(func.distinct(LaoqinPunch.emp_id)))
        .where(LaoqinPunch.natual_date >= date_from)
        .where(LaoqinPunch.natual_date <= date_to)
    )
    matched_employee_stmt = (
        select(func.count(func.distinct(LaoqinPunch.emp_id)))
        .select_from(LaoqinPunch)
        .join(Employee, (Employee.emp_id == LaoqinPunch.emp_id) & (Employee.deleted.is_(False)))
        .where(LaoqinPunch.natual_date >= date_from)
        .where(LaoqinPunch.natual_date <= date_to)
    )
    recon_count_stmt = select(func.count()).select_from(ReconResult).where(
        ReconResult.date.in_(dates)
    )
    if requested_codes is not None:
        total_stmt = total_stmt.where(LaoqinPunch.emp_id.in_(requested_codes))
        matched_employee_stmt = matched_employee_stmt.where(
            LaoqinPunch.emp_id.in_(requested_codes)
        )
        recon_count_stmt = recon_count_stmt.where(ReconResult.emp_id.in_(requested_codes))
    total_employees = db.scalar(total_stmt) or 0
    matched_employees = db.scalar(matched_employee_stmt) or 0
    matched = db.scalar(recon_count_stmt) or 0

    return {
        "dateFrom": date_from, "dateTo": date_to,
        "rawCount": agg["rawCount"], "inserted": agg["inserted"], "skipped": agg["skipped"],
        "skippedNoUser": agg["skippedNoUser"],  # 原始打卡中「无用户信息(不在花名册)」被忽略的条数
        "removedNoUser": agg["removedNoUser"],  # 清理掉的历史「无用户信息」打卡条数
        "dates": dates,
        "eligibleDates": len(eligible),
        "eligibleEmployeeDays": sum(len(v) for v in eligible.values()),
        "matched": matched,                    # 对账匹配条数（=进入劳勤工作台）
        "totalEmployees": total_employees,     # 范围内 laoqin_punch 涉及的不同工号数
        "matchedEmployees": matched_employees,  # 其中在花名册里的工号数
    }


# ── 劳勤转换后台任务（按天推进 + 进度状态，复用 dingtalk_sync_state 表）────────────
def _state_kind(kind: str, region: str | None = None) -> str:
    if not region:
        return kind
    # kind 列只有 32 字符；地区原文保留在 current_root，键使用稳定摘要隔离状态行。
    digest_length = min(10, 32 - len(kind) - 1)
    if digest_length < 8:
        raise ValueError(f"同步状态 kind 前缀过长：{kind}")
    digest = hashlib.sha1(region.encode("utf-8")).hexdigest()[:digest_length]
    state_kind = f"{kind}:{digest}"
    assert len(state_kind) <= 32
    return state_kind


def _task_state(
    db: Session, kind: str, region: str | None = None
) -> DingtalkSyncState:
    state_kind = _state_kind(kind, region)
    st = db.scalar(select(DingtalkSyncState).where(DingtalkSyncState.kind == state_kind))
    if not st:
        st = DingtalkSyncState(kind=state_kind, status="idle", current_root=region)
        db.add(st)
        try:
            db.commit()
        except IntegrityError:
            # 地区状态行按首次轮询惰性创建；并发请求可能同时观察到“不存在”。
            # 唯一键冲突说明另一请求已创建成功，回滚后重查即可。
            db.rollback()
            st = db.scalar(
                select(DingtalkSyncState).where(DingtalkSyncState.kind == state_kind)
            )
            if st is None:
                raise
    return st


def _transform_state(db: Session, region: str | None = None) -> DingtalkSyncState:
    return _task_state(db, TRANSFORM_KIND, region)


def _claim_task_lease(db: Session) -> str | None:
    """原子抢占跨进程劳勤任务租约；进程锁只保留为同进程快速拒绝。"""
    _task_state(db, TASK_LEASE_KIND)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=STALE_RUNNING_MINUTES)
    owner_token = str(uuid.uuid4())
    result = db.execute(
        update(DingtalkSyncState)
        .where(
            DingtalkSyncState.kind == TASK_LEASE_KIND,
            or_(
                DingtalkSyncState.owner_token.is_(None),
                DingtalkSyncState.updated_at.is_(None),
                DingtalkSyncState.updated_at < cutoff,
            ),
        )
        .values(
            owner_token=owner_token,
            status="running",
            message="劳勤同步/转换任务持有数据库租约",
            started_at=now,
            finished_at=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return owner_token if result.rowcount == 1 else None


def _heartbeat_task_lease(db: Session, owner_token: str) -> None:
    """续约只能由当前 owner 完成；租约已被接管时让旧任务尽快停止。"""
    result = db.execute(
        update(DingtalkSyncState)
        .where(
            DingtalkSyncState.kind == TASK_LEASE_KIND,
            DingtalkSyncState.owner_token == owner_token,
        )
        .values(updated_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise TaskLeaseLost("劳勤任务数据库租约已失效")
    db.commit()


def _release_task_lease(db: Session, owner_token: str) -> None:
    """条件释放，绝不清掉另一个进程已经接管的新租约。"""
    now = datetime.now(timezone.utc)
    db.execute(
        update(DingtalkSyncState)
        .where(
            DingtalkSyncState.kind == TASK_LEASE_KIND,
            DingtalkSyncState.owner_token == owner_token,
        )
        .values(
            owner_token=None,
            status="idle",
            message="劳勤任务数据库租约已释放",
            finished_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()


def _task_lease_active(db: Session) -> bool:
    lease = db.scalar(
        select(DingtalkSyncState).where(DingtalkSyncState.kind == TASK_LEASE_KIND)
    )
    if lease is None or not lease.owner_token or lease.updated_at is None:
        return False
    updated_at = lease.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return updated_at >= datetime.now(timezone.utc) - timedelta(
        minutes=STALE_RUNNING_MINUTES
    )


def _transform_update(db: Session, st: DingtalkSyncState, **fields) -> None:
    for k, v in fields.items():
        setattr(st, k, v)
    st.updated_at = datetime.now(timezone.utc)
    db.commit()


def _status_payload(st: DingtalkSyncState) -> dict:
    total = st.dept_total or 0
    done = st.dept_done or 0
    return {
        "status": st.status,                 # idle / running / done / error
        "phase": st.phase,                   # 当前日期
        "total": total,                      # 总天数
        "done": done,                        # 已完成天数
        "percent": round(done * 100 / total) if total else 0,
        "message": st.message,
        "startedAt": as_shanghai_datetime(st.started_at).isoformat() if st.started_at else None,
        "finishedAt": as_shanghai_datetime(st.finished_at).isoformat() if st.finished_at else None,
        "updatedAt": as_shanghai_datetime(st.updated_at).isoformat() if st.updated_at else None,
    }


def _recover_stale_running_states(db: Session, kind: str) -> int:
    """把无心跳超过阈值的 running 状态收敛为 error，避免进程中断后永久锁死 UI。"""
    # 以数据库租约判断共享任务是否仍存活，不依赖当前 worker 的 threading.Lock。
    # 因此多 uvicorn worker 或独立调度进程查询状态时不会误杀别的进程的任务。
    if _task_lease_active(db):
        return 0
    # 同进程兜底：任务锁仍被持有时说明拉取/转换确实在推进（例如某个重日超过
    # 心跳间隔），不能因租约一时无刷新就在本进程内自杀式误判为中断。
    if _transform_lock.locked():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_RUNNING_MINUTES)
    rows = list(
        db.scalars(
            select(DingtalkSyncState).where(
                or_(
                    DingtalkSyncState.kind == kind,
                    DingtalkSyncState.kind.like(f"{kind}:%"),
                ),
                DingtalkSyncState.status == "running",
                or_(
                    DingtalkSyncState.updated_at.is_(None),
                    DingtalkSyncState.updated_at < cutoff,
                ),
            )
        ).all()
    )
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    for row in rows:
        row.status = "error"
        row.message = "同步任务超过 30 分钟无心跳，已判定中断；可重新发起"
        row.finished_at = now
        row.updated_at = now
    db.commit()
    logger.warning("已恢复 %s 条僵尸同步状态（kind=%s）", len(rows), kind)
    return len(rows)


def _scoped_status(db: Session, kind: str, region: str | None) -> dict:
    _recover_stale_running_states(db, kind)
    st = _task_state(db, kind, region)
    # 本地区失败必须保留，不能被后来其它地区的任务覆盖或伪装成 idle。
    if region and st.status not in ("error", "running", "done"):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_RUNNING_MINUTES)
        running = db.scalar(
            select(DingtalkSyncState).where(
                DingtalkSyncState.kind != st.kind,
                or_(
                    DingtalkSyncState.kind == kind,
                    DingtalkSyncState.kind.like(f"{kind}:%"),
                ),
                DingtalkSyncState.status == "running",
                DingtalkSyncState.updated_at >= cutoff,
            )
        )
        if running:
            payload = _status_payload(running)
            payload["message"] = (
                "全局定时任务进行中" if running.current_root is None
                else "其他地区同步任务占用中"
            )
            return payload
    return _status_payload(st)


def get_transform_status(db: Session, region: str | None = None) -> dict:
    """劳勤转换进度（前端轮询）。total=天数(dept_total)，done=已完成天数(dept_done)。"""
    return _scoped_status(db, TRANSFORM_KIND, region)


def get_attendance_result_status(db: Session, region: str | None = None) -> dict:
    """劳勤结果视图拉取进度（前端轮询）。"""
    return _scoped_status(db, ATT_RESULT_KIND, region)


@logged_sync("attendance_transform")
def _run_transform(
    date_from: str,
    date_to: str,
    codes: list[str] | None = None,
    region: str | None = None,
    lease_token: str | None = None,
    actor: str = "admin",
) -> None:
    """后台线程：按天转换 + 推进度。独立 Session，结束释放锁。"""
    db = SessionLocal()
    try:
        st = _transform_state(db, region)
        dates = _date_list(date_from, date_to)
        _transform_update(
            db, st, status="running", phase=date_from, dept_total=len(dates), dept_done=0,
            message=f"开始转换 {date_from} ~ {date_to}（共 {len(dates)} 天）",
            current_root=region,
            started_at=datetime.now(timezone.utc), finished_at=None,
        )

        def cb(done, total, d):
            _transform_update(
                db, st, phase=d, dept_done=done, dept_total=total,
                message=f"已完成 {d}（{done}/{total} 天）",
            )
            if lease_token:
                _heartbeat_task_lease(db, lease_token)

        res = (
            transform_range(db, date_from, date_to, progress_cb=cb, codes=codes)
            if codes is not None
            else transform_range(db, date_from, date_to, progress_cb=cb)
        )
        _transform_update(
            db, st, status="done", phase=date_to, dept_done=len(dates),
            finished_at=datetime.now(timezone.utc),
            message=(f"转换完成：新增 {res['inserted']} 条、跳过 {res['skipped']} 条、"
                     f"对账 {res['matched']} 条，进入劳勤工作台"),
        )
    except TaskLeaseLost as e:
        mark("failed", str(e))
        try:
            st = _transform_state(db, region)
            _transform_update(db, st, status="error", finished_at=datetime.now(timezone.utc),
                              message=f"任务租约已失效，本次转换停止：{e}（可能已被其他进程接管）")
        except Exception:  # noqa: BLE001
            logger.exception("写入劳勤转换租约失效状态失败", extra={"region": region})
    except Exception as e:  # noqa: BLE001
        mark("failed", str(e))
        try:
            st = _transform_state(db, region)
            _transform_update(db, st, status="error", finished_at=datetime.now(timezone.utc),
                              message=f"转换失败：{e}")
        except Exception:  # noqa: BLE001
            logger.exception("写入地区劳勤转换失败状态时再次失败", extra={"region": region})
    finally:
        if lease_token:
            try:
                _release_task_lease(db, lease_token)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("释放劳勤转换数据库租约失败")
        db.close()
        _transform_lock.release()


@logged_start("attendance_transform")
def start_transform(
    db: Session,
    date_from: str,
    date_to: str,
    codes: list[str] | None = None,
    region: str | None = None,
    actor: str = "admin",
) -> dict:
    """非阻塞启动后台劳勤转换；已在跑则返回 alreadyRunning。"""
    _validate_date(date_from)
    _validate_date(date_to)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    if not _transform_lock.acquire(blocking=False):
        return {"started": False, "alreadyRunning": True}
    try:
        lease_token = _claim_task_lease(db)
    except Exception:
        _transform_lock.release()
        raise
    if lease_token is None:
        _transform_lock.release()
        return {"started": False, "alreadyRunning": True}
    try:
        threading.Thread(
            target=_run_transform,
            args=(date_from, date_to, codes, region, lease_token, actor),
            daemon=True,
        ).start()
    except Exception:
        _release_task_lease(db, lease_token)
        _transform_lock.release()
        raise
    return {"started": True, "alreadyRunning": False}


@logged_sync("attendance_pull")
def _run_pull(
    date_from: str,
    date_to: str,
    actor: str,
    codes: list[str] | None = None,
    region: str | None = None,
    lease_token: str | None = None,
) -> None:
    """后台线程：按工作点员工日拉取原始考勤、转换并记录进度。"""
    db = SessionLocal()
    try:
        st = _transform_state(db, region)
        requested_codes = ({c for c in codes if c} if codes is not None else None)
        eligible = _eligible_codes_by_date(
            db, date_from, date_to, requested_codes=requested_codes
        )
        employee_days = sum(len(v) for v in eligible.values())
        source_total = len(_date_list(date_from, date_to)) + 2
        _transform_update(
            db, st, status="running", phase=date_from, dept_total=source_total, dept_done=0,
            message=f"开始按工作点员工日拉取并转换 {date_from} ~ {date_to}（共 {employee_days} 个员工日）",
            current_root=region,
            started_at=datetime.now(timezone.utc), finished_at=None,
        )

        def cb(done, total, date):
            # total 随阶段增长（拉取天数 → 拉取+转换总天数），同步抬升 dept_total，
            # 保证进度百分比全程单调不回退。
            _transform_update(db, st, phase=date, dept_done=done, dept_total=total,
                              message=f"正在处理 {date}（{done}/{total}）")
            if lease_token:
                _heartbeat_task_lease(db, lease_token)

        try:
            res = (
                pull_range(db, date_from, date_to, progress_cb=cb, codes=codes)
                if codes is not None
                else pull_range(db, date_from, date_to, progress_cb=cb)
            )
        except TaskLeaseLost as e:
            mark("failed", str(e))
            # 租约失效与业务失败分开记录，避免被误读成「源库故障，重跑即可」。
            db.rollback()
            _transform_update(db, st, status="error", finished_at=datetime.now(timezone.utc),
                              message=f"任务租约已失效，本次拉取停止：{e}（可能已被其他进程接管）")
            _log_sync(db, actor=actor, trigger="manual", result={
                "dateFrom": date_from, "dateTo": date_to,
                "region": region, "synced": None, "transformed": None, "error": str(e)})
            return
        except Exception as e:  # noqa: BLE001 — 源库不可达/某天失败：状态置错 + 记日志
            mark("failed", str(e))
            db.rollback()
            _transform_update(db, st, status="error", finished_at=datetime.now(timezone.utc),
                              message=f"拉取失败：{e}")
            _log_sync(db, actor=actor, trigger="manual", result={
                "dateFrom": date_from, "dateTo": date_to,
                "region": region, "synced": None, "transformed": None, "error": str(e)})
            return
        s, t = res["synced"], res["transformed"]
        completed_total = s.get("sourceDates", source_total)
        _transform_update(
            db, st, status="done", phase=date_to, dept_total=completed_total, dept_done=completed_total,
            finished_at=datetime.now(timezone.utc),
            message=(f"拉取完成：拉取 {s['fetched']} 条、入库 {s['upserted']} 条；"
                     f"转劳勤新增 {t['inserted']} 条、对账 {t['matched']} 条"),
        )
        _log_sync(db, actor=actor, trigger="manual", result={
            "dateFrom": date_from, "dateTo": date_to,
            "region": region, "synced": s, "transformed": t, "error": None})
    except Exception as e:  # noqa: BLE001 — 状态机自身异常兜底，不留 running 僵尸状态
        mark("failed", str(e))
        try:
            st = _transform_state(db, region)
            _transform_update(db, st, status="error", finished_at=datetime.now(timezone.utc),
                              message=f"拉取失败：{e}")
        except Exception:  # noqa: BLE001
            logger.exception("写入地区考勤结果拉取失败状态时再次失败", extra={"region": region})
    finally:
        if lease_token:
            try:
                _release_task_lease(db, lease_token)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("释放劳勤拉取数据库租约失败")
        db.close()
        _transform_lock.release()


@logged_start("attendance_pull")
def start_pull(
    db: Session,
    date_from: str,
    date_to: str,
    actor: str = "admin",
    codes: list[str] | None = None,
    region: str | None = None,
) -> dict:
    """非阻塞启动「按工作点员工日拉取+转劳勤」后台任务；与转换共用一把锁。"""
    _validate_date(date_from)
    _validate_date(date_to)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    if not _transform_lock.acquire(blocking=False):
        return {"started": False, "alreadyRunning": True}
    try:
        lease_token = _claim_task_lease(db)
    except Exception:
        _transform_lock.release()
        raise
    if lease_token is None:
        _transform_lock.release()
        return {"started": False, "alreadyRunning": True}
    try:
        threading.Thread(
            target=_run_pull,
            args=(date_from, date_to, actor, codes, region, lease_token),
            daemon=True,
        ).start()
    except Exception:
        _release_task_lease(db, lease_token)
        _transform_lock.release()
        raise
    return {"started": True, "alreadyRunning": False}


@logged_sync("attendance_result")
def _run_attendance_result_pull(
    date_from: str,
    date_to: str,
    codes: list[str] | None = None,
    region: str | None = None,
    lease_token: str | None = None,
    actor: str = "admin",
) -> None:
    """后台线程：按工作点员工月拉取劳勤考勤结果视图。"""
    db = SessionLocal()
    try:
        st = _task_state(db, ATT_RESULT_KIND, region)
        requested_codes = ({c for c in codes if c} if codes is not None else None)
        eligible = _eligible_result_codes_by_date(
            db, date_from, date_to, requested_codes=requested_codes
        )
        employee_days = sum(len(v) for v in eligible.values())
        date_total = len(_date_list(date_from, date_to))
        _transform_update(
            db, st, status="running", phase=date_from, dept_total=date_total, dept_done=0,
            message=f"开始按工作点员工月范围拉取考勤结果 {date_from} ~ {date_to}（共 {employee_days} 个员工日）",
            current_root=region,
            started_at=datetime.now(timezone.utc), finished_at=None,
        )

        def cb(done, total, date):
            _transform_update(db, st, phase=date, dept_done=done, dept_total=total,
                              message=f"考勤结果读取/提交进度 {date}（{done}/{total}）")
            if lease_token:
                _heartbeat_task_lease(db, lease_token)

        try:
            res = (
                pull_attendance_result_range(
                    db, date_from, date_to, progress_cb=cb, codes=codes
                )
                if codes is not None
                else pull_attendance_result_range(
                    db, date_from, date_to, progress_cb=cb
                )
            )
        except Exception as e:  # noqa: BLE001
            mark("failed", str(e))
            db.rollback()
            _transform_update(db, st, status="error", finished_at=datetime.now(timezone.utc),
                              message=f"拉取考勤结果失败：{e}")
            return
        s = res["synced"]
        completed_total = len(res.get("dates") or [])
        _transform_update(
            db, st, status="done", phase=date_to, dept_total=completed_total, dept_done=completed_total,
            finished_at=datetime.now(timezone.utc),
            message=f"考勤结果拉取完成：拉取 {s['fetched']} 条、入库 {s['upserted']} 条",
        )
    except Exception as e:  # noqa: BLE001
        mark("failed", str(e))
        try:
            st = _task_state(db, ATT_RESULT_KIND, region)
            _transform_update(db, st, status="error", finished_at=datetime.now(timezone.utc),
                              message=f"拉取考勤结果失败：{e}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        if lease_token:
            try:
                _release_task_lease(db, lease_token)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("释放考勤结果拉取数据库租约失败")
        db.close()
        _transform_lock.release()


@logged_start("attendance_result")
def start_attendance_result_pull(
    db: Session,
    date_from: str,
    date_to: str,
    codes: list[str] | None = None,
    region: str | None = None,
    actor: str = "admin",
) -> dict:
    """非阻塞启动劳勤考勤结果视图拉取；与原始拉取/转换共用锁。"""
    _validate_date(date_from)
    _validate_date(date_to)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    if not _transform_lock.acquire(blocking=False):
        return {"started": False, "alreadyRunning": True}
    try:
        lease_token = _claim_task_lease(db)
    except Exception:
        _transform_lock.release()
        raise
    if lease_token is None:
        _transform_lock.release()
        return {"started": False, "alreadyRunning": True}
    try:
        threading.Thread(
            target=_run_attendance_result_pull,
            args=(date_from, date_to, codes, region, lease_token, actor),
            daemon=True,
        ).start()
    except Exception:
        _release_task_lease(db, lease_token)
        _transform_lock.release()
        raise
    return {"started": True, "alreadyRunning": False}


def list_raw(
    db: Session,
    date_from: str,
    date_to: str,
    limit: int = 500,
    *,
    codes: list[str] | None = None,
    employee_id: str | None = None,
    employee_name: str | None = None,
) -> dict:
    """按日期范围列出原始考勤；工号、姓名均支持包含式模糊搜索。"""
    _validate_date(date_from)
    _validate_date(date_to)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    base_stmt = (
        select(DingtalkAttendanceRaw)
        .where(DingtalkAttendanceRaw.natual_date >= date_from)
        .where(DingtalkAttendanceRaw.natual_date <= date_to)
    )
    if codes is not None:
        base_stmt = base_stmt.where(DingtalkAttendanceRaw.code.in_(codes))
    employee_id = (employee_id or "").strip()
    employee_name = (employee_name or "").strip()
    if employee_id:
        base_stmt = base_stmt.where(
            DingtalkAttendanceRaw.code.contains(employee_id, autoescape=True)
        )
    if employee_name:
        base_stmt = base_stmt.where(
            DingtalkAttendanceRaw.name.contains(employee_name, autoescape=True)
        )
    total = db.scalar(select(func.count()).select_from(base_stmt.subquery())) or 0
    rows = db.scalars(
        base_stmt
        .order_by(DingtalkAttendanceRaw.att_time.desc())
        .limit(max(1, min(limit, 2000)))
    ).all()
    return {
        "dateFrom": date_from, "dateTo": date_to, "total": total, "count": len(rows),
        "items": [
            {
                "id": r.id,
                "code": r.code,
                "name": r.name,
                "org": r.org,
                "layer": r.layer,
                "attTime": as_shanghai_datetime(r.att_time).strftime("%Y-%m-%d %H:%M:%S") if r.att_time else None,
                "direct": r.direct,
                "state": r.state,
                "src": r.src,
                "syncedAt": as_shanghai_datetime(r.synced_at).strftime("%Y-%m-%d %H:%M:%S") if r.synced_at else None,
            }
            for r in rows
        ],
    }


def list_attendance_result(
    db: Session,
    date_from: str,
    date_to: str,
    limit: int = 500,
    *,
    user_code: str | None = None,
    user_name: str | None = None,
    dept_name: str | None = None,
    codes: list[str] | None = None,
) -> dict:
    """按 kq_date 范围列出已同步的劳勤考勤结果，支持工号精确、姓名/部门模糊筛选。"""
    _validate_date(date_from)
    _validate_date(date_to)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    stmt = (
        select(ExternalAttendanceResultRaw)
        .where(ExternalAttendanceResultRaw.kq_date >= date_from)
        .where(ExternalAttendanceResultRaw.kq_date <= date_to)
    )
    user_code = (user_code or "").strip()
    user_name = (user_name or "").strip()
    dept_name = (dept_name or "").strip()
    if user_code:
        stmt = stmt.where(ExternalAttendanceResultRaw.user_code == user_code)
    if user_name:
        stmt = stmt.where(ExternalAttendanceResultRaw.user_name.ilike(f"%{user_name}%"))
    if dept_name:
        stmt = stmt.where(ExternalAttendanceResultRaw.dept_name.ilike(f"%{dept_name}%"))
    if codes is not None:
        stmt = stmt.where(ExternalAttendanceResultRaw.user_code.in_(codes))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt
        .order_by(ExternalAttendanceResultRaw.kq_date.desc(), ExternalAttendanceResultRaw.user_code)
        .limit(max(1, min(limit, 2000)))
    ).all()
    return {
        "dateFrom": date_from, "dateTo": date_to, "total": total, "count": len(rows),
        "items": [
            {
                "id": r.id,
                "tenantCode": r.tenant_code,
                "deptName": r.dept_name,
                "layerName": r.layer_name,
                "hrStatus": r.hr_status,
                "hrType": r.hr_type,
                "userCode": r.user_code,
                "userName": r.user_name,
                "kqDateShow": r.kq_date_show,
                "kqDateTitle": r.kq_date_title,
                "kqDate": r.kq_date,
                "kqqs": r.kqqs,
                "statusName": r.status_name,
                "sourceWithdrawnAt": as_shanghai_datetime(r.source_withdrawn_at).strftime("%Y-%m-%d %H:%M:%S") if r.source_withdrawn_at else None,
                "lxType": r.lx_type,
                "actualOn": r.actual_on,
                "actualOff": r.actual_off,
                "quota": r.quota,
                "cqgs": r.cqgs,
                "kggs": r.kggs,
                "cd2": r.cd2,
                "zt2": r.zt2,
                "ot1": r.ot1,
                "ot2": r.ot2,
                "ot3": r.ot3,
                "leave01": r.leave_01,
                "leave02": r.leave_02,
                "leave03": r.leave_03,
                "leave04": r.leave_04,
                "leave05": r.leave_05,
                "leave06": r.leave_06,
                "leave07": r.leave_07,
                "leave09": r.leave_09,
                "leave10": r.leave_10,
                "leave12": r.leave_12,
                "leave13": r.leave_13,
                "ext1": r.ext1,
                "ext2": r.ext2,
                "business01": r.business_01,
                "business02": r.business_02,
                "syncedAt": as_shanghai_datetime(r.synced_at).strftime("%Y-%m-%d %H:%M:%S") if r.synced_at else None,
            }
            for r in rows
        ],
    }


# ── 定时任务：每天 0 点自动拉取并转换上月全月 + 本月至今考勤 ─────────────────
def _attendance_month_ranges(today: str) -> list[tuple[str, str]]:
    d = datetime.strptime(today, "%Y-%m-%d").date()
    cur_start = d.replace(day=1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    return [(prev_start.isoformat(), prev_end.isoformat()), (cur_start.isoformat(), d.isoformat())]


def _sum_dicts(rows: list[dict], keys: tuple[str, ...]) -> dict:
    return {k: sum(int((r or {}).get(k) or 0) for r in rows) for k in keys}


def _furnace_labels(db: Session, furnace_ids: list[int]) -> str:
    """工作点 id → 『地区 工作点编号』可读列表，供封账跳过提示使用。"""
    from app.models import Furnace

    if not furnace_ids:
        return ""
    furnaces = db.scalars(
        select(Furnace).where(Furnace.id.in_(furnace_ids))
    ).all()
    labels = sorted({f"{f.region} {f.furnace_no}" for f in furnaces})
    return "、".join(labels) if labels else ",".join(str(fid) for fid in furnace_ids)


@logged_sync("attendance_auto", trigger="auto")
def attendance_sync_job(today: str | None = None, db: Session | None = None) -> dict:
    """每日 0 点定时：同步上月全月 + 本月 1 号到今日的两张劳勤源表。

    原始打卡表走 pull_range（同步 dingtalk_attendance_raw 并转 laoqin_punch/对账）；
    考勤结果表走 pull_attendance_result_range（同步 external_attendance_result_raw）。
    两者都按有效/待审工作点员工日拉取，共用一把锁避免并发写。
    内网 MySQL 不可达/未配密码时优雅降级（记日志、不抛，避免拖垮调度器）。
    独立 Session（db 参数供测试注入）。today 参数供测试注入。"""
    from zoneinfo import ZoneInfo

    from app.services.monthly_export_lock_service import (
        all_active_employee_codes,
        locked_furnace_employee_codes,
        locked_furnace_ids_for_range,
    )

    own = db is None
    if own:
        db = SessionLocal()
    try:
        if today is None:
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        ranges = _attendance_month_ranges(today)
        date_from, date_to = ranges[0][0], ranges[-1][1]
        result = {
            "dateFrom": date_from,
            "dateTo": date_to,
            "ranges": [],
            "synced": None,
            "attendanceResult": None,
            "transformed": None,
            "error": None,
        }
        lease_token = None
        if not _transform_lock.acquire(blocking=False):
            result["error"] = "已有拉取/转换任务进行中，本次定时同步跳过（上月全月+本月至今，次日自动补齐）"
        else:
            state = None
            last_phase = date_from
            completed_ranges = 0
            try:
                lease_token = _claim_task_lease(db)
                if lease_token is None:
                    result["error"] = "其他进程已有拉取/转换任务进行中，本次定时同步跳过"
                    _log_sync(db, actor="scheduler", trigger="auto", result=result)
                    return result
                state = _transform_state(db)
                _transform_update(
                    db,
                    state,
                    status="running",
                    phase=date_from,
                    dept_total=len(ranges),
                    dept_done=0,
                    current_root=None,
                    message=f"定时同步 {date_from} ~ {date_to}",
                    started_at=datetime.now(timezone.utc),
                    finished_at=None,
                )
                range_results = []

                def heartbeat(done, total, phase):
                    _transform_update(
                        db,
                        state,
                        phase=phase,
                        message=f"定时同步处理中：{phase}（{done}/{total}）",
                    )
                    _heartbeat_task_lease(db, lease_token)

                skipped_locked_total = 0
                for index, (dfrom, dto) in enumerate(ranges, start=1):
                    # 工作点级月锁收窄：月份内有工作点封账时，只排除「封账工作点对应员工」，
                    # 其余工作点照常同步；仅当范围内员工全部来自封账工作点时才整段跳过。
                    locked_ids = locked_furnace_ids_for_range(db, dfrom, dto)
                    sync_codes: list[str] | None = None
                    locked_codes: set[str] = set()
                    if locked_ids:
                        locked_codes = locked_furnace_employee_codes(db, dfrom, dto, locked_ids)
                        all_codes = all_active_employee_codes(db)
                        sync_codes = sorted(all_codes - locked_codes)
                    if locked_ids and not sync_codes:
                        skipped_locked_total += 1
                        labels = _furnace_labels(db, locked_ids)
                        range_results.append({
                            "dateFrom": dfrom,
                            "dateTo": dto,
                            "synced": {"fetched": 0, "deduped": 0, "upserted": 0, "employeeCount": 0},
                            "transformed": {"rawCount": 0, "inserted": 0, "skipped": 0, "skippedNoUser": 0, "removedNoUser": 0, "matched": 0},
                            "attendanceResult": {"fetched": 0, "deduped": 0, "upserted": 0},
                            "skippedLocked": True,
                            "lockedFurnaces": labels,
                            "error": f"{dfrom[:7]} 全部工作点（{labels}）月度导出已锁定，整段跳过；锁定期间不拉取，解锁后次日自动补齐",
                        })
                        last_phase = dto
                        completed_ranges = index
                        _transform_update(
                            db,
                            state,
                            phase=dto,
                            dept_done=index,
                            message=f"定时同步已跳过封账工作点范围 {index}/{len(ranges)}",
                        )
                        _heartbeat_task_lease(db, lease_token)
                        continue
                    raw_res = pull_range(db, dfrom, dto, progress_cb=heartbeat, codes=sync_codes)
                    att_res = pull_attendance_result_range(
                        db, dfrom, dto, progress_cb=heartbeat, codes=sync_codes
                    )
                    range_entry = {
                        "dateFrom": dfrom,
                        "dateTo": dto,
                        "synced": raw_res["synced"],
                        "transformed": raw_res["transformed"],
                        "attendanceResult": att_res["synced"],
                    }
                    if locked_ids:
                        range_entry["narrowedForLockedFurnaces"] = _furnace_labels(db, locked_ids)
                        range_entry["excludedEmployeeCount"] = len(locked_codes)
                    range_results.append(range_entry)
                    last_phase = dto
                    completed_ranges = index
                    _transform_update(
                        db,
                        state,
                        phase=dto,
                        dept_done=index,
                        message=f"定时同步已完成 {index}/{len(ranges)} 个月份范围",
                    )
                    _heartbeat_task_lease(db, lease_token)
                result["ranges"] = range_results
                result["synced"] = _sum_dicts(
                    [r["synced"] for r in range_results],
                    ("fetched", "deduped", "upserted", "employeeCount"),
                )
                result["transformed"] = _sum_dicts(
                    [r["transformed"] for r in range_results],
                    ("rawCount", "inserted", "skipped", "skippedNoUser", "removedNoUser", "matched"),
                )
                result["attendanceResult"] = _sum_dicts(
                    [r["attendanceResult"] for r in range_results],
                    ("fetched", "deduped", "upserted"),
                )
                # 封账导致的跳过/收窄必须进入顶层结果：收尾状态与审计日志不能把
                # 「部分没同步」呈现成毫无保留的「定时同步完成」。
                result["skippedLockedRanges"] = sum(
                    1 for r in range_results if r.get("skippedLocked")
                )
                result["narrowedLockedRanges"] = sum(
                    1 for r in range_results if r.get("narrowedForLockedFurnaces")
                )
            except Exception as e:  # noqa: BLE001 — 源库不可达/未配密码等：降级不崩
                result["error"] = str(e)
            finally:
                # 即使整段同步失败，也尝试逐员工日补拉；复用同一租约保持串行。
                if lease_token:
                    try:
                        db.rollback()
                        from app.services.attendance_resync_service import run_daily
                        result["bulkResync"] = run_daily(db, date_from, date_to, today, lease_token)
                    except Exception as exc:
                        db.rollback()
                        result["error"] = "; ".join(filter(None, [result["error"], f"异常补拉失败：{exc}"]))
                try:
                    if state is not None:
                        # 上游数据库异常可能让当前事务处于 failed 状态；先回滚，再按 kind
                        # 重查状态行。收尾字段只用本地快照，避免读取 expired ORM 属性。
                        db.rollback()
                        terminal_state = db.scalar(
                            select(DingtalkSyncState).where(
                                DingtalkSyncState.kind == TRANSFORM_KIND
                            )
                        )
                        if terminal_state is None:
                            terminal_state = _transform_state(db)
                        _transform_update(
                            db,
                            terminal_state,
                            status="done" if result["error"] is None else "error",
                            phase=date_to if result["error"] is None else last_phase,
                            dept_done=(
                                len(ranges)
                                if result["error"] is None
                                else completed_ranges
                            ),
                            message=(
                                _finalize_sync_message(result)
                                if result["error"] is None
                                else f"定时同步失败：{result['error']}"
                            ),
                            finished_at=datetime.now(timezone.utc),
                        )
                except Exception as state_error:  # noqa: BLE001
                    # 状态收尾失败不能穿透定时任务、跳过审计日志或拖垮调度器。
                    try:
                        db.rollback()
                    except Exception:  # noqa: BLE001
                        logger.exception("定时考勤同步状态收尾失败后回滚也失败")
                    logger.exception("定时考勤同步状态收尾失败")
                    detail = f"状态收尾失败：{state_error}"
                    result["error"] = (
                        f"{result['error']}；{detail}" if result["error"] else detail
                    )
                finally:
                    if lease_token:
                        try:
                            _release_task_lease(db, lease_token)
                        except Exception:  # noqa: BLE001
                            db.rollback()
                            logger.exception("释放定时考勤同步数据库租约失败")
                    _transform_lock.release()
        _log_sync(db, actor="scheduler", trigger="auto", result=result)
        return result
    finally:
        if own:
            db.close()


def _finalize_sync_message(result: dict) -> str:
    """收尾文案：封账跳过/收窄不能缺席，避免「部分没同步」被读成「全部完成」。"""
    skipped = int(result.get("skippedLockedRanges") or 0)
    narrowed = int(result.get("narrowedLockedRanges") or 0)
    if skipped and narrowed:
        return f"定时同步完成（{skipped} 段封账跳过、{narrowed} 段按未封账工作点收窄）"
    if skipped:
        return f"定时同步完成（{skipped} 个月份范围因封账整段跳过）"
    if narrowed:
        return f"定时同步完成（{narrowed} 个月份范围已按未封账工作点收窄）"
    return "定时同步完成"


def _log_sync(db: Session, *, actor: str, trigger: str, result: dict) -> None:
    """把一次同步结果写入 audit_log（action=attendance_auto_sync），供「同步日志」标签查看。
    写日志失败不应影响主流程（回滚吞掉）。"""
    from app.models import AuditLog

    s = result.get("synced") or {}
    ar = result.get("attendanceResult") or {}
    t = result.get("transformed") or {}
    payload = {
        "trigger": trigger,  # auto=定时 / manual=手动
        "dateFrom": result.get("dateFrom"),
        "dateTo": result.get("dateTo"),
        # 手动地区任务必须把服务端确定的地区带到审计日志。同步日志接口据此做
        # 权限过滤；缺失时地区 HR 会把一次成功同步误看成“没有执行”。
        "region": result.get("region"),
        "ranges": result.get("ranges"),
        "fetched": s.get("fetched"),
        "upserted": s.get("upserted"),
        "attendanceResultFetched": ar.get("fetched"),
        "attendanceResultUpserted": ar.get("upserted"),
        "transformInserted": t.get("inserted"),
        "matched": t.get("matched"),
        "skippedLockedRanges": result.get("skippedLockedRanges") or 0,
        "narrowedLockedRanges": result.get("narrowedLockedRanges") or 0,
        "bulkResync": result.get("bulkResync"),
        "ok": result.get("error") is None,
        "error": result.get("error"),
    }
    try:
        db.add(AuditLog(actor=actor, action="attendance_auto_sync",
                        payload=json.dumps(payload, ensure_ascii=False)))
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("写入考勤同步审计日志失败")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.exception("考勤同步日志写入失败后回滚也失败")


def reset_all_laoqin_data(
    db: Session,
    *,
    actor: str,
    fail_if_locked: bool = False,
) -> dict:
    """清空全部劳勤源/转换数据，并把对账收敛到仍有工作点记录的员工日。

    这是一次显式维护操作；AttendanceRecord、Employee、人工确认仍相关的 ReconResult
    和既有审计日志不会被清空。月锁默认解除；时区维护可要求有月锁时拒绝执行。
    """
    locked_count = db.scalar(
        select(func.count())
        .select_from(MonthlyExportLock)
        .where(MonthlyExportLock.locked.is_(True))
    ) or 0
    if fail_if_locked and locked_count:
        raise RuntimeError(
            f"检测到 {locked_count} 个已封账工作点，拒绝清理考勤数据；请先确认封账处理方案"
        )

    before = {
        "raw": db.scalar(select(func.count()).select_from(DingtalkAttendanceRaw)) or 0,
        "punches": db.scalar(select(func.count()).select_from(LaoqinPunch)) or 0,
        "attendanceResults": db.scalar(
            select(func.count()).select_from(ExternalAttendanceResultRaw)
        )
        or 0,
        "recon": db.scalar(select(func.count()).select_from(ReconResult)) or 0,
    }

    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(
            text(
                "TRUNCATE TABLE dingtalk_attendance_raw, laoqin_punch, "
                "external_attendance_result_raw RESTART IDENTITY"
            )
        )
    else:
        db.execute(delete(DingtalkAttendanceRaw))
        db.execute(delete(LaoqinPunch))
        db.execute(delete(ExternalAttendanceResultRaw))

    eligible = exists(
        select(AttendanceRecord.id)
        .join(Employee, Employee.emp_id == AttendanceRecord.emp_id)
        .where(
            AttendanceRecord.emp_id == ReconResult.emp_id,
            AttendanceRecord.natual_date == ReconResult.date,
            AttendanceRecord.status.in_(ELIGIBLE_ATTENDANCE_STATUSES),
            Employee.deleted.is_(False),
        )
    )
    removed_recon = int(db.execute(delete(ReconResult).where(~eligible)).rowcount or 0)

    unlocked_months: set[str] = set()
    for lock in db.scalars(select(MonthlyExportLock).where(MonthlyExportLock.locked.is_(True))):
        lock.locked = False
        lock.unlocked_by = actor
        lock.unlocked_at = datetime.now(timezone.utc)
        unlocked_months.add(lock.month)

    for state in db.scalars(
        select(DingtalkSyncState).where(
            or_(
                DingtalkSyncState.kind == TRANSFORM_KIND,
                DingtalkSyncState.kind.like(f"{TRANSFORM_KIND}:%"),
                DingtalkSyncState.kind == ATT_RESULT_KIND,
                DingtalkSyncState.kind.like(f"{ATT_RESULT_KIND}:%"),
            )
        )
    ):
        state.status = "idle"
        state.phase = None
        state.dept_total = 0
        state.dept_done = 0
        state.message = "劳勤历史数据已清理，等待按工作点员工日重建"
        state.finished_at = datetime.now(timezone.utc)
        state.updated_at = datetime.now(timezone.utc)

    lease = db.scalar(
        select(DingtalkSyncState).where(DingtalkSyncState.kind == TASK_LEASE_KIND)
    )
    if lease is not None:
        lease.owner_token = None
        lease.status = "idle"
        lease.message = "劳勤历史数据已清理，数据库租约已重置"
        lease.finished_at = datetime.now(timezone.utc)
        lease.updated_at = datetime.now(timezone.utc)

    db.add(
        AuditLog(
            actor=actor,
            action="attendance_filtered_reset",
            payload=json.dumps(
                {
                    "before": before,
                    "removedRecon": removed_recon,
                    "unlockedMonths": sorted(unlocked_months),
                    "requiredNoLocks": fail_if_locked,
                },
                ensure_ascii=False,
            ),
        )
    )
    db.commit()

    bounds = db.execute(
        select(
            func.min(AttendanceRecord.natual_date),
            func.max(AttendanceRecord.natual_date),
            func.count(func.distinct(AttendanceRecord.natual_date)),
        )
        .join(Employee, Employee.emp_id == AttendanceRecord.emp_id)
        .where(
            AttendanceRecord.status.in_(ELIGIBLE_ATTENDANCE_STATUSES),
            Employee.deleted.is_(False),
        )
    ).one()
    return {
        "before": before,
        "removedRecon": removed_recon,
        "remainingRecon": before["recon"] - removed_recon,
        "unlockedMonths": sorted(unlocked_months),
        "eligibleDateFrom": bounds[0],
        "eligibleDateTo": bounds[1],
        "eligibleDates": int(bounds[2] or 0),
    }
