import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models import (
    AttendanceRecord,
    AuditLog,
    ChiefWorker,
    Employee,
    EmployeeFavorite,
    Furnace,
    FurnaceChief,
    LaoqinPunch,
    MonthlyExportLock,
    ReconResult,
    Region,
)
from app.scan_active import ATTENDANCE_KIND_OVERTIME, scan_is_active
from app.services.recon_service import (
    get_base_recon_for,
    get_overtime_recon_status,
    run_recon_for_date,
)

# 筛选用的确认态（下拉），含展示态
CONFIRM_STATES = ("通过", "不通过", "待值班长确认", "补录待值班长确认", "未确认")
# HR 可设状态：第四态「待值班长确认」= 委托值班长，值班长通过即班次有效
HR_CONFIRM_STATES = ("通过", "不通过", "待值班长确认", "未确认")
# 值班长可设状态；「补录待值班长确认」由系统对待审补录自动标记，不可手设
CHIEF_CONFIRM_STATES = ("通过", "不通过", "未确认")
CHIEF_DISPLAY_STATES = ("通过", "不通过", "补录待值班长确认", "未确认")
PENDING_MAKEUP_CHIEF = "补录待值班长确认"
# 全部对账状态（劳勤工作台全量展示，含「自动一致」）
RECON_STATUSES = ("自动一致", "时间错位", "工作点有劳勤无", "劳勤有工作点无", "加班需核对")
MATCHABLE_SQUADS = ("1班", "2班", "3班")


def derive_validity(
    *, status: str, hr_status: str, chief_status: str, pending_makeup: bool
) -> tuple[bool, str]:
    """是否可用（班次有效/无效）+ 确认理由 推导规则：

    - 自动一致（无问题）→ 有效，理由「自动一致」；但 HR/值班长 任一「不通过」→ 无效。
      值班长代签与已获值班长通过的补录同扫码权限，钉钉覆盖即自动一致。
    - 问题（时间错位、工作点有劳勤无、劳勤有工作点无、补录待值班长确认）→ 自动无效，理由即问题；
      此时 HR「通过」→ 有效（理由 部门确认通过）；
      HR「待值班长确认」且 值班长「通过」→ 有效（理由 值班长确认通过）。
    """
    problem: str | None = None
    if status == "加班需核对":
        if hr_status == "不通过":
            return False, "部门确认不通过"
        if chief_status == "不通过":
            return False, "值班长不通过"
        if hr_status == "通过":
            return True, "部门确认通过"
        if chief_status == "通过":
            return True, "值班长确认通过"
        return False, "加班需核对"
    if status in ("时间错位", "工作点有劳勤无", "劳勤有工作点无"):
        problem = status
    elif pending_makeup:
        problem = PENDING_MAKEUP_CHIEF

    if problem is None:
        if hr_status == "不通过":
            return False, "部门确认不通过"
        if chief_status == "不通过":
            return False, "值班长不通过"
        return True, "自动一致"
    if hr_status == "通过":
        return True, "部门确认通过"
    if hr_status == "待值班长确认" and chief_status == "通过":
        return True, "值班长确认通过"
    return False, problem


def derive_overtime_validity(
    *,
    status: str,
    hr_status: str,
    chief_status: str,
    base_valid: bool,
    matching_base_squad: str | None,
) -> tuple[bool, str]:
    """独立加班有效性：先尊重独立打卡覆盖，再允许有效正常班为同班组加班兜底。"""
    if (
        status == "加班需核对"
        and base_valid
        and matching_base_squad in MATCHABLE_SQUADS
    ):
        return (
            True,
            f"正式班已通过且加班班组一致（{matching_base_squad}），自动通过",
        )
    return derive_validity(
        status=status,
        hr_status=hr_status,
        chief_status=chief_status,
        pending_makeup=False,
    )


def derive_overtime_result(
    *,
    status: str,
    hr_status: str,
    chief_status: str,
    base_valid: bool,
    matching_base_squad: str | None,
) -> tuple[str, bool, str]:
    """返回加班最终展示状态；同班组正常班兜底通过时状态同步为自动一致。"""
    valid, reason = derive_overtime_validity(
        status=status,
        hr_status=hr_status,
        chief_status=chief_status,
        base_valid=base_valid,
        matching_base_squad=matching_base_squad,
    )
    effective_status = (
        "自动一致"
        if status == "加班需核对"
        and base_valid
        and matching_base_squad in MATCHABLE_SQUADS
        else status
    )
    return effective_status, valid, reason


def get_overtime_validity(
    db: Session,
    overtime: AttendanceRecord,
    recon_result: ReconResult | None = None,
) -> tuple[str, bool, str]:
    """按统一口径返回单条加班的最终对账状态、有效性和理由。"""
    recon = recon_result or db.scalar(
        select(ReconResult).where(
            ReconResult.emp_id == overtime.emp_id,
            ReconResult.date == overtime.natual_date,
        )
    )
    hr_status = recon.hr_status if recon else "未确认"
    chief_status = recon.chief_status if recon else "未确认"
    base_records = list(
        db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.emp_id == overtime.emp_id,
                AttendanceRecord.natual_date == overtime.natual_date,
                AttendanceRecord.record_kind != ATTENDANCE_KIND_OVERTIME,
                AttendanceRecord.status.in_(["active", "待审"]),
            )
        ).all()
    )
    active_base = [record for record in base_records if record.status == "active"]
    pending_makeup = not active_base and any(
        record.status == "待审" and record.source == "补录"
        for record in base_records
    )
    base_status = get_base_recon_for(db, overtime.emp_id, overtime.natual_date)["status"]
    base_valid, _ = derive_validity(
        status=base_status,
        hr_status=hr_status,
        chief_status=chief_status,
        pending_makeup=pending_makeup,
    )
    matching_base_squad = (
        overtime.squad
        if overtime.squad in MATCHABLE_SQUADS
        and any(record.squad == overtime.squad for record in base_records)
        else None
    )
    status = get_overtime_recon_status(db, overtime)
    return derive_overtime_result(
        status=status,
        hr_status=hr_status,
        chief_status=chief_status,
        base_valid=base_valid,
        matching_base_squad=matching_base_squad,
    )


def get_demo_context() -> dict[str, str]:
    """真实时间（按 Asia/Shanghai 取当地日期，避免容器 UTC 跨零点偏差）：today/昨天/前天。"""
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return {
        "t2": (today - timedelta(days=2)).isoformat(),
        "t1": (today - timedelta(days=1)).isoformat(),
        "today": today.isoformat(),
        "month": today.strftime("%Y-%m"),
    }


def resolve_demo_date(date: str) -> str:
    ctx = get_demo_context()
    u = date.upper()
    if u == "T-2":
        return ctx["t2"]
    if u == "T-1":
        return ctx["t1"]
    if u in ("TODAY", "今日", "今天"):
        return ctx["today"]
    return date


def _parse_detail(detail: str | None) -> dict:
    if not detail:
        return {}
    try:
        return json.loads(detail)
    except json.JSONDecodeError:
        return {}


def _resolve_emp_id(db: Session, raw_id: str) -> tuple[str, str, str | None]:
    """Map emp_id or dd_user_id → (emp_id, display_name, furnace_no)."""
    emp = db.scalar(select(Employee).where(Employee.emp_id == raw_id))
    if not emp:
        emp = db.scalar(select(Employee).where(Employee.dd_user_id == raw_id))
    if emp:
        return emp.emp_id, emp.name, None
    return raw_id, raw_id, None


def _furnace_id_for_emp(db: Session, emp_id: str, date: str) -> int | None:
    """该员工当日所在工作点的 furnace_id（全局唯一，用于跨地区精确的归属判定）。
    优先扫码 → 补录/代签 → 无则 None。与 _furnace_for_emp 的考勤记录部分同源，
    但不包含仅供旧展示使用的 LaoqinPunch.org_name 工作点编号关键字兜底；返回 id
    不返回工作点编号，避免「同名工作点编号跨地区」误命中（值班长越权确认他地区同名工作点员工）。"""
    sr = db.scalar(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            scan_is_active(),
            AttendanceRecord.record_kind != ATTENDANCE_KIND_OVERTIME,
        )
        .order_by(
            AttendanceRecord.scan_time.is_(None).asc(),
            AttendanceRecord.scan_time.desc(),
            AttendanceRecord.id.desc(),
        )
    )
    if sr:
        return sr.furnace_id
    # 无 active 出勤时按任一考勤记录（待审补录/代签等）定位工作点
    mk = db.scalar(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.record_kind != ATTENDANCE_KIND_OVERTIME,
        )
        .order_by(AttendanceRecord.id)
    )
    if mk:
        return mk.furnace_id
    return None


def resolve_review_furnace_id(
    db: Session,
    emp_id: str,
    date: str,
    recon: ReconResult | None = None,
    attendance_records: list[AttendanceRecord] | None = None,
) -> int | None:
    """解析值班长待办工作点，并在固化委托证据失效时回落到当前考勤。

    委托后新增另一工作点的有效出勤时，只要原工作点仍有有效基础出勤，仍保持已通知
    的委托工作点；原工作点证据已失效、当前有效出勤又全部落到其它地区时，旧固化值
    不再授予写权限，改按当前考勤解析，避免原地区值班长越权确认外地区员工日。
    """
    if recon is None:
        recon = db.scalar(
            select(ReconResult).where(
                ReconResult.emp_id == emp_id,
                ReconResult.date == date,
            )
        )
    records = (
        [
            record
            for record in attendance_records
            if record.record_kind != ATTENDANCE_KIND_OVERTIME
        ]
        if attendance_records is not None
        else None
    )
    if records is None:
        records = db.scalars(
            select(AttendanceRecord)
            .where(
                AttendanceRecord.emp_id == emp_id,
                AttendanceRecord.natual_date == date,
                AttendanceRecord.record_kind != ATTENDANCE_KIND_OVERTIME,
            )
            .order_by(AttendanceRecord.id)
        ).all()

    active = [record for record in records if record.status == "active"]
    current_evidence = active or list(records)
    current_furnace_id: int | None = None
    if active:
        timed = [record for record in active if record.scan_time is not None]
        if timed:
            current_furnace_id = max(
                timed, key=lambda record: (record.scan_time, record.id)
            ).furnace_id
        else:
            current_furnace_id = max(active, key=lambda record: record.id).furnace_id
    elif records:
        current_furnace_id = min(records, key=lambda record: record.id).furnace_id

    delegate_furnace_id = (
        recon.delegate_furnace_id if recon is not None else None
    )
    if delegate_furnace_id is None:
        return current_furnace_id
    if not current_evidence:
        # 历史记录已无可用考勤时保留已审计的委托目标，不凭空改派。
        return delegate_furnace_id
    evidence_furnace_ids = {record.furnace_id for record in current_evidence}
    if delegate_furnace_id in evidence_furnace_ids:
        return delegate_furnace_id
    delegate_furnace = db.get(Furnace, delegate_furnace_id)
    evidence_regions = {
        furnace.region
        for furnace_id in evidence_furnace_ids
        if (furnace := db.get(Furnace, furnace_id)) is not None
    }
    if delegate_furnace and delegate_furnace.region in evidence_regions:
        # 同地区换工作点仍保留已通知待办；权限边界没有跨地区扩大。
        return delegate_furnace_id
    return current_furnace_id


def _furnace_id_for_emp_in_region(
    db: Session, emp_id: str, date: str, region: str
) -> int | None:
    """员工当日在指定地区的工作点 furnace_id；优先 active 扫码，兜底任一考勤记录。

    与 get_anomalies 的地区裁剪同口径：读路径只要求本地区存在任一工作点证据即保留
    该员工日，写路径因此也能定位到本地区工作点做**地区可见性**校验，
    避免「看得见行、点确认却 403」。员工当日无本地区工作点证据时返回 None。

    该回退只用于地区可见性判定，不得用于收窄月锁校验范围：确认写入的是
    (emp_id, date) 唯一的 ReconResult，会同时改写外区已封账月份的导出口径。
    """
    sr = db.scalar(
        select(AttendanceRecord)
        .join(Furnace, Furnace.id == AttendanceRecord.furnace_id)
        .where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            scan_is_active(),
            AttendanceRecord.record_kind != ATTENDANCE_KIND_OVERTIME,
            Furnace.region == region,
        )
        .order_by(AttendanceRecord.scan_time.desc())
    )
    if sr:
        return sr.furnace_id
    any_active_base = db.scalar(
        select(AttendanceRecord.id).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            scan_is_active(),
            AttendanceRecord.record_kind != ATTENDANCE_KIND_OVERTIME,
        )
    )
    if any_active_base:
        return None
    mk = db.scalar(
        select(AttendanceRecord)
        .join(Furnace, Furnace.id == AttendanceRecord.furnace_id)
        .where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.status == "待审",
            AttendanceRecord.source.in_(("补录", "代签")),
            Furnace.region == region,
        )
    )
    if mk:
        return mk.furnace_id
    return None


def furnace_ids_for_emp_on_date(db: Session, emp_id: str, date: str) -> list[int]:
    """员工当日会受确认结果影响的工作点（active/待审，含基础出勤与加班）。"""
    rows = db.scalars(
        select(AttendanceRecord.furnace_id).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.furnace_id.is_not(None),
            AttendanceRecord.status.in_(("active", "待审")),
        ).distinct()
    ).all()
    return [int(fid) for fid in rows if fid is not None]


def _furnace_for_emp(db: Session, emp_id: str, date: str) -> str:
    sr = db.scalar(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            scan_is_active(),
            AttendanceRecord.record_kind != ATTENDANCE_KIND_OVERTIME,
        )
        .order_by(
            AttendanceRecord.scan_time.is_(None).asc(),
            AttendanceRecord.scan_time.desc(),
            AttendanceRecord.id.desc(),
        )
    )
    if sr:
        f = db.get(Furnace, sr.furnace_id)
        if f:
            return f.furnace_no
    # 补录/代签：无 active 出勤时按任一考勤记录定位工作点
    mk = db.scalar(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.record_kind != ATTENDANCE_KIND_OVERTIME,
        )
        .order_by(AttendanceRecord.id)
    )
    if mk:
        f = db.get(Furnace, mk.furnace_id)
        if f:
            return f.furnace_no
    lp = db.scalar(
        select(LaoqinPunch)
        .where(LaoqinPunch.emp_id == emp_id, LaoqinPunch.natual_date == date)
        .order_by(LaoqinPunch.punch_time.desc())
    )
    if lp and lp.org_name:
        for token in ("35号工作点", "1号工作点", "10号工作点"):
            if token in lp.org_name:
                return token
    return "—"


def _chief_name_map(db: Session) -> dict[int, dict]:
    """furnace_id → 首个在职值班长。按 id 不按工作点编号，避免跨地区同名工作点编号取错。"""
    emp_by = {
        e.emp_id: e
        for e in db.scalars(
            select(Employee).where(
                Employee.deleted.is_(False),
                Employee.employment_status != "departed",
            )
        ).all()
    }
    out: dict[int, dict] = {}
    for fc in db.scalars(
        select(FurnaceChief).where(FurnaceChief.deleted.is_(False)).order_by(FurnaceChief.id)
    ).all():
        if fc.furnace_id not in out and (e := emp_by.get(fc.emp_id)) is not None:
            out[fc.furnace_id] = {
                "name": e.name,
                "empId": fc.emp_id,
                "ddUserId": e.dd_user_id or "",
            }
    return out


def _data_source(db: Session, emp_id: str, date: str) -> str:
    """数据来源：当天扫码 / 本人补录 / 值班长代签 / —（无扫码）。"""
    sr = db.scalar(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            scan_is_active(),
        )
        .order_by(AttendanceRecord.scan_time)
    )
    if sr:
        src = sr.source or "扫码"
        if src == "扫码":
            return "当天扫码"
        return "值班长代签" if src == "代签" else "本人补录"
    # 无 active 出勤：按待审/拒绝的考勤主张区分（代签/补录）
    mk = db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.source.in_(["补录", "代签"]),
        )
    )
    if mk:
        return "值班长代签" if mk.source == "代签" else "本人补录"
    return "—"


def get_anomalies(
    db: Session,
    date: str | None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    filters: dict | None = None,
    restrict_furnace_ids: set[int] | None = None,
    page: int = 1,
    page_size: int = 50,
    paginate: bool = True,
    employee_id: str | None = None,
) -> dict:
    """劳勤工作台：返回对账记录（含自动一致），支持多维筛选。
    date 为空且 date_from/date_to 也为空=全量（所有日期）；
    给定 date=仅该日；给定 date_from/date_to=按区间（含两端）。"""
    from app.services.report_snapshot import ReportSnapshot
    from app.services.report_jobs import bounded_rows, check_report_deadline

    if date:
        date_from = date_to = resolve_demo_date(date)
    source_conditions = [AttendanceRecord.status.in_(["active", "待审"])]
    if date_from:
        source_conditions.append(AttendanceRecord.natual_date >= date_from)
    if date_to:
        source_conditions.append(AttendanceRecord.natual_date <= date_to)
    if employee_id:
        source_conditions.append(AttendanceRecord.emp_id == employee_id)
    name_filter = (filters or {}).get("name")
    if name_filter:
        # 姓名/工号必须在读取报表源数据前收窄。仅在最终展示行过滤会先把
        # 整个地区整月的数据读入快照，单人查询也会触发源行数上限。
        matching_employees = select(Employee.emp_id).where(
            Employee.name.contains(name_filter, autoescape=True)
        )
        source_conditions.append(or_(
            AttendanceRecord.emp_id.contains(name_filter, autoescape=True),
            AttendanceRecord.emp_id.in_(matching_employees),
        ))
    scope_furnaces = None
    if restrict_furnace_ids is not None:
        scope_furnaces = set(restrict_furnace_ids)
    elif (filters or {}).get("region") or (filters or {}).get("furnace"):
        furnace_query = select(Furnace.id)
        if (filters or {}).get("region"):
            furnace_query = furnace_query.where(Furnace.region == filters["region"])
        if (filters or {}).get("furnace"):
            furnace_query = furnace_query.where(Furnace.furnace_no == filters["furnace"])
        scope_furnaces = set(bounded_rows(db, furnace_query))
    if scope_furnaces is not None:
        delegated = select(ReconResult.emp_id).where(ReconResult.delegate_furnace_id.in_(scope_furnaces))
        if date_from:
            delegated = delegated.where(ReconResult.date >= date_from)
        if date_to:
            delegated = delegated.where(ReconResult.date <= date_to)
        source_conditions.append(or_(
            AttendanceRecord.furnace_id.in_(scope_furnaces),
            AttendanceRecord.emp_id.in_(delegated),
        ))
    selected_employees = set(bounded_rows(db, select(AttendanceRecord.emp_id).where(
        *source_conditions
    ).distinct()))
    if not date_from or not date_to:
        first, last = db.execute(select(
            func.min(AttendanceRecord.natual_date), func.max(AttendanceRecord.natual_date)
        ).where(*source_conditions)).one()
        date_from, date_to = date_from or first, date_to or last
    # An empty source range still yields the usual response/filter shape below.
    context = ReportSnapshot(db, date_from=date_from or "1970-01-01",
                             date_to=date_to or "1970-01-01", employee_ids=selected_employees)
    results = sorted(context.recons.values(), key=lambda r: (r.date, r.emp_id), reverse=True)
    visible_furnace_ids = set(context.furnaces) | {
        r.delegate_furnace_id for r in results if r.delegate_furnace_id is not None
    }
    furnace_chiefs = bounded_rows(db, select(FurnaceChief).where(
        FurnaceChief.furnace_id.in_(visible_furnace_ids), FurnaceChief.deleted.is_(False),
    ).order_by(FurnaceChief.id))
    worker_chiefs = bounded_rows(db, select(ChiefWorker).where(
        ChiefWorker.emp_id.in_(selected_employees),
    ).order_by(ChiefWorker.id))
    display_employee_ids = selected_employees | {r.emp_id for r in furnace_chiefs} | {
        r.chief_user_id for r in worker_chiefs
    }
    emp_by = {e.emp_id: e for e in bounded_rows(db, select(Employee).where(
        Employee.emp_id.in_(display_employee_ids),
    ))}
    chief_by_furnace = {}
    for fc in furnace_chiefs:
        e = emp_by.get(fc.emp_id)
        if e and not e.deleted and e.employment_status != "departed":
            chief_by_furnace.setdefault(fc.furnace_id, {
                "name": e.name, "empId": e.emp_id, "ddUserId": e.dd_user_id or "",
            })
    # 地区范围（HR/统计由服务端强制传入）：行构造时即按地区裁剪展示字段，
    # 避免跨地区工作点/值班长/工时混入受限地区 HR 的工作台。
    scoped_region = (filters or {}).get("region")

    def _furnace_region(fid: int | None) -> str | None:
        fobj = furnaces_by_id.get(fid) if fid is not None else None
        return fobj.region if fobj else None

    # 工人 → 其绑定的「常用值班长」(chief_worker)：取第一条绑定
    bound_chief_by_emp: dict[str, dict] = {}
    for cw in worker_chiefs:
        if cw.emp_id not in bound_chief_by_emp:
            ce = emp_by.get(cw.chief_user_id)
            bound_chief_by_emp[cw.emp_id] = {
                "name": ce.name if ce else cw.chief_user_id,
                "empId": cw.chief_user_id,
                "ddUserId": (ce.dd_user_id if ce else "") or "",
            }

    # 批量预载（消除逐行 10+ 查询）：dd_user_id 解析、工作点字典、当日考勤/打卡分组
    emp_by_dd = {e.dd_user_id: e for e in emp_by.values() if e.dd_user_id}
    furnaces_by_id = {f.id: f for f in bounded_rows(db, select(Furnace).where(
        Furnace.id.in_(visible_furnace_ids),
    ))}
    result_dates = {r.date for r in results if r.date}
    locked_furnace_ids_by_month: dict[str, set[int]] = {}
    if result_dates:
        months = {d[:7] for d in result_dates if d}
        for lock in bounded_rows(db,
            select(MonthlyExportLock).where(
                MonthlyExportLock.furnace_id.in_(visible_furnace_ids),
                MonthlyExportLock.month.in_(months),
                MonthlyExportLock.locked.is_(True),
            )
        ):
            locked_furnace_ids_by_month.setdefault(lock.month, set()).add(lock.furnace_id)
    att_by = context.records_by_key
    laoqin_org_by = {
        key: [p.org_name for p in punches]
        for key, punches in context.punches_by_key.items() if key[1] in result_dates
    }
    attendance_result_by = context.results

    rows: list[dict] = []
    for r in results:
        rdate = r.date  # 全量模式下每行用各自日期
        e = emp_by.get(r.emp_id) or emp_by_dd.get(r.emp_id)
        emp_id = e.emp_id if e else r.emp_id
        name = e.name if e else r.emp_id
        atts = att_by.get((emp_id, rdate), [])
        active = [a for a in atts if a.status == "active"]
        active_scan = [a for a in active if a.record_kind != ATTENDANCE_KIND_OVERTIME]
        active_overtime = [a for a in active if a.record_kind == ATTENDANCE_KIND_OVERTIME]
        scans_sorted = sorted(active_scan, key=lambda a: (a.scan_time is None, a.scan_time))
        # 基础出勤行只展示正常出勤归属；加班另拆行，不污染基础出勤工作点/值班长。
        base_records_for_row = active_scan or [
            a for a in atts if a.record_kind != ATTENDANCE_KIND_OVERTIME
        ]
        delegate_furnace_id = resolve_review_furnace_id(
            db, emp_id, rdate, r, attendance_records=atts
        )
        delegate_furnace = (
            furnaces_by_id.get(delegate_furnace_id)
            if delegate_furnace_id is not None
            else None
        )
        delegate_out_of_scope = bool(
            restrict_furnace_ids is None
            and scoped_region
            and delegate_furnace
            and delegate_furnace.region != scoped_region
        )
        visible_delegate_furnace_id = (
            None if delegate_out_of_scope else delegate_furnace_id
        )
        visible_delegate_region = (
            ""
            if delegate_out_of_scope or delegate_furnace is None
            else delegate_furnace.region
        )
        visible_delegate_furnace = (
            ""
            if delegate_out_of_scope or delegate_furnace is None
            else delegate_furnace.furnace_no
        )
        delegate_chief = (
            {}
            if delegate_out_of_scope or delegate_furnace_id is None
            else chief_by_furnace.get(delegate_furnace_id, {})
        )
        fur_pairs: list[dict] = []
        furnace_chiefs: list[dict] = []
        seen_fid: set[int] = set()
        seen_chief: set[str] = set()
        for a in base_records_for_row:
            fid = a.furnace_id
            if fid in seen_fid:
                continue
            seen_fid.add(fid)
            fobj = furnaces_by_id.get(fid)
            if fobj:
                fur_pairs.append({"region": fobj.region, "furnace": fobj.furnace_no})
            ch = chief_by_furnace.get(fid)
            if ch and ch.get("empId") and ch["empId"] not in seen_chief:
                seen_chief.add(ch["empId"])
                furnace_chiefs.append({"name": ch.get("name", ""), "empId": ch["empId"]})
        scan_n = len(active_scan)
        laoqin_n = len(laoqin_org_by.get((emp_id, rdate), []))
        # 补录问题：考勤主张来自「待审」本人补录（无真实扫码）→ 待值班长确认
        pending_makeup = scan_n == 0 and any(
            a.status == "待审" and a.source == "补录" for a in atts
        )
        attendance_result = attendance_result_by.get((emp_id, rdate))
        check_report_deadline()
        recon_now = context.base(emp_id, rdate)
        valid, reason = derive_validity(
            status=recon_now["status"],
            hr_status=r.hr_status,
            chief_status=r.chief_status,
            pending_makeup=pending_makeup,
        )
        eligible_base_squads = {
            record.squad
            for record in atts
            if record.record_kind != ATTENDANCE_KIND_OVERTIME
            and record.status in ("active", "待审")
            and record.squad in MATCHABLE_SQUADS
        }
        # 有效性细分：自动/手动 × 有效/无效
        manual_reasons = {"部门确认通过", "值班长确认通过", "部门确认不通过", "值班长不通过"}
        valid_kind = ("手动" if reason in manual_reasons else "自动") + ("有效" if valid else "无效")
        # 基础出勤行只统计正常出勤；加班记录另拆行。
        total_hours = float(sum(a.work_hours or 0.0 for a in active_scan))
        ot_hours = 0.0
        type_sums: dict[str, float] = {}
        for a in active_scan:
            type_sums[a.employ_type] = type_sums.get(a.employ_type, 0.0) + float(a.work_hours or 0.0)
        formal_hours = round(type_sums.get("正式", 0.0), 2)
        intern_hours = round(type_sums.get("实习", 0.0), 2)
        standby_hours = round(type_sums.get("待岗", 0.0), 2)
        # 工作点号/工作点id：最近有效扫码 → 任一考勤记录 → 钉钉组织名token → —/None
        furnace_no = "—"
        furnace_id = None
        if scans_sorted:
            latest = scans_sorted[-1]
            furnace_id = latest.furnace_id
            fobj = furnaces_by_id.get(latest.furnace_id)
            furnace_no = fobj.furnace_no if fobj else "—"
        elif base_records_for_row:
            furnace_id = base_records_for_row[0].furnace_id
            fobj = furnaces_by_id.get(base_records_for_row[0].furnace_id)
            furnace_no = fobj.furnace_no if fobj else "—"
        else:
            for org in laoqin_org_by.get((emp_id, rdate), []):
                hit = next((t for t in ("35号工作点", "1号工作点", "10号工作点") if org and t in org), None)
                if hit:
                    furnace_no = hit
                    break
        # 数据来源：最早有效扫码的 source → 待审/拒绝的补录/代签 → —
        if scans_sorted:
            src = scans_sorted[0].source or "扫码"
            data_source = (
                "当天扫码" if src == "扫码" else ("值班长代签" if src == "代签" else "本人补录")
            )
        else:
            mk = next((a for a in atts if a.source in ("补录", "代签")), None)
            data_source = ("值班长代签" if mk.source == "代签" else "本人补录") if mk else "—"
        row = {
            "empId": emp_id,
            "rowKey": f"{emp_id}-{rdate}-recon",
            "rowKind": "recon",
            "name": name,
            "workHours": total_hours,
            "overtimeHours": ot_hours,
            "formalHours": formal_hours,
            "internHours": intern_hours,
            "standbyHours": standby_hours,
            "furnaces": fur_pairs,
            "date": rdate,
            "region": (
                furnaces_by_id[furnace_id].region
                if furnace_id in furnaces_by_id
                else ""
            ),
            "furnace": furnace_no,
            "furnaceId": furnace_id,
            "delegateFurnaceId": visible_delegate_furnace_id,
            "delegateRegion": visible_delegate_region,
            "delegateFurnace": visible_delegate_furnace,
            "delegateOutOfScope": delegate_out_of_scope,
            "delegateChiefName": delegate_chief.get("name", ""),
            "delegateChiefEmpId": delegate_chief.get("empId", ""),
            "delegateChiefDdUserId": delegate_chief.get("ddUserId", ""),
            "furnaceChiefs": furnace_chiefs,
            "chiefName": (chief_by_furnace.get(furnace_id, {}).get("name", "") if furnace_id else ""),
            "chiefEmpId": (chief_by_furnace.get(furnace_id, {}).get("empId", "") if furnace_id else ""),
            "chiefDdUserId": (chief_by_furnace.get(furnace_id, {}).get("ddUserId", "") if furnace_id else ""),
            # 工人绑定的常用值班长（chief_worker）；无则 None
            "boundChief": bound_chief_by_emp.get(emp_id),
            "dataSource": data_source,
            "status": recon_now["status"],
            "hrStatus": r.hr_status,
            "chiefStatus": r.chief_status,
            # 待审补录且值班长尚未确认 → 展示「补录待值班长确认」（人工设过则用实际值）
            "chiefStatusDisplay": (
                PENDING_MAKEUP_CHIEF
                if pending_makeup and r.chief_status == "未确认"
                else r.chief_status
            ),
            "pendingMakeup": pending_makeup,
            "hasScan": scan_n > 0,
            "laoqinCount": int(laoqin_n),
            "laoqinWorkHours": recon_now.get("laoqinWorkHours"),
            "attendanceResultWorkHours": attendance_result.cqgs if attendance_result else None,
            "valid": valid,
            "validReason": reason,
            "validKind": valid_kind,
            "frozenByLock": any(
                a.furnace_id in locked_furnace_ids_by_month.get(rdate[:7], set())
                for a in base_records_for_row
            ),
        }
        if scoped_region:
            # 字段级裁剪：同一员工当天跨地区有记录时，受限地区 HR 只看到本地区
            # 的工作点/值班长/工时，地区隔离不能只做到「整行保留」。
            scoped_scan = [
                a for a in scans_sorted if _furnace_region(a.furnace_id) == scoped_region
            ]
            scoped_pairs = [
                p for p in fur_pairs if p.get("region") == scoped_region
            ]
            has_outside = (
                len(scoped_scan) != len(scans_sorted)
                or len(scoped_pairs) != len(fur_pairs)
                or any(
                    _furnace_region(a.furnace_id) != scoped_region
                    for a in base_records_for_row
                )
            )
            if has_outside and (scoped_scan or scoped_pairs):
                scoped_base = [
                    a
                    for a in base_records_for_row
                    if _furnace_region(a.furnace_id) == scoped_region
                ]
                scoped_chiefs: list[dict] = []
                seen_scoped_chief: set[str] = set()
                for a in scoped_base:
                    ch = chief_by_furnace.get(a.furnace_id)
                    if ch and ch.get("empId") and ch["empId"] not in seen_scoped_chief:
                        seen_scoped_chief.add(ch["empId"])
                        scoped_chiefs.append(
                            {"name": ch.get("name", ""), "empId": ch["empId"]}
                        )
                if scoped_scan:
                    latest = scoped_scan[-1]
                    scoped_fid = latest.furnace_id
                elif scoped_base:
                    scoped_fid = scoped_base[0].furnace_id
                else:
                    scoped_fid = None
                fobj = furnaces_by_id.get(scoped_fid) if scoped_fid is not None else None
                scoped_type_sums: dict[str, float] = {}
                for a in scoped_scan:
                    scoped_type_sums[a.employ_type] = scoped_type_sums.get(
                        a.employ_type, 0.0
                    ) + float(a.work_hours or 0.0)
                if scoped_scan:
                    src = scoped_scan[0].source or "扫码"
                    scoped_source = (
                        "当天扫码"
                        if src == "扫码"
                        else ("值班长代签" if src == "代签" else "本人补录")
                    )
                else:
                    mk = next(
                        (a for a in scoped_base if a.source in ("补录", "代签")), None
                    )
                    scoped_source = (
                        ("值班长代签" if mk.source == "代签" else "本人补录")
                        if mk
                        else "—"
                    )
                row.update(
                    {
                        "furnaces": scoped_pairs,
                        "region": fobj.region if fobj else "",
                        "furnace": fobj.furnace_no if fobj else "—",
                        "furnaceId": scoped_fid,
                        "furnaceChiefs": scoped_chiefs,
                        "chiefName": (
                            chief_by_furnace.get(scoped_fid, {}).get("name", "")
                            if scoped_fid
                            else ""
                        ),
                        "chiefEmpId": (
                            chief_by_furnace.get(scoped_fid, {}).get("empId", "")
                            if scoped_fid
                            else ""
                        ),
                        "chiefDdUserId": (
                            chief_by_furnace.get(scoped_fid, {}).get("ddUserId", "")
                            if scoped_fid
                            else ""
                        ),
                        "workHours": float(
                            sum(a.work_hours or 0.0 for a in scoped_scan)
                        ),
                        "formalHours": round(scoped_type_sums.get("正式", 0.0), 2),
                        "internHours": round(scoped_type_sums.get("实习", 0.0), 2),
                        "standbyHours": round(scoped_type_sums.get("待岗", 0.0), 2),
                        "dataSource": scoped_source,
                        "hasScan": bool(scoped_scan),
                        # 跨地区员工日裁剪标记：status/valid 等仍是整日对账口径，
                        # 前端可据此提示「对账状态按全天判定」。
                        "crossRegion": True,
                        # 劳勤结果/劳勤打卡工时是整员工日口径：与裁剪后的本地区
                        # workHours 并列会形成「工作点 8h、劳勤 12h」的自相矛盾组合，
                        # 诱导 HR 把正确记录误改成不一致。裁剪行置空，避免误读。
                        "attendanceResultWorkHours": None,
                        "laoqinWorkHours": None,
                    }
                )
        rows.append(row)
        for ot in active_overtime:
            fobj = furnaces_by_id.get(ot.furnace_id)
            ch = chief_by_furnace.get(ot.furnace_id, {})
            ot_status = context.overtime_status(ot)
            ot_status, ot_valid, ot_reason = derive_overtime_result(
                status=ot_status,
                hr_status=r.hr_status,
                chief_status=r.chief_status,
                base_valid=valid,
                matching_base_squad=(
                    ot.squad if ot.squad in eligible_base_squads else None
                ),
            )
            ot_valid_kind = (
                "手动" if ot_reason in manual_reasons else "自动"
            ) + ("有效" if ot_valid else "无效")
            ot_hours = float(ot.work_hours or 0.0)
            rows.append(
                {
                    "empId": emp_id,
                    "rowKey": f"{emp_id}-{rdate}-overtime-{ot.id}",
                    "rowKind": "overtime",
                    "overtimeId": ot.id,
                    "name": name,
                    "workHours": ot_hours,
                    "overtimeHours": ot_hours,
                    "formalHours": round(ot_hours if ot.employ_type == "正式" else 0.0, 2),
                    "internHours": round(ot_hours if ot.employ_type == "实习" else 0.0, 2),
                    "standbyHours": round(ot_hours if ot.employ_type == "待岗" else 0.0, 2),
                    "furnaces": (
                        [{"region": fobj.region, "furnace": fobj.furnace_no}] if fobj else []
                    ),
                    "date": rdate,
                    "region": fobj.region if fobj else "",
                    "furnace": fobj.furnace_no if fobj else "—",
                    "furnaceId": ot.furnace_id,
                    "delegateFurnaceId": visible_delegate_furnace_id,
                    "delegateRegion": visible_delegate_region,
                    "delegateFurnace": visible_delegate_furnace,
                    "delegateOutOfScope": delegate_out_of_scope,
                    "delegateChiefName": delegate_chief.get("name", ""),
                    "delegateChiefEmpId": delegate_chief.get("empId", ""),
                    "delegateChiefDdUserId": delegate_chief.get("ddUserId", ""),
                    "furnaceChiefs": (
                        [{"name": ch.get("name", ""), "empId": ch.get("empId", "")}]
                        if ch.get("empId")
                        else []
                    ),
                    "chiefName": ch.get("name", ""),
                    "chiefEmpId": ch.get("empId", ""),
                    "chiefDdUserId": ch.get("ddUserId", ""),
                    "boundChief": bound_chief_by_emp.get(emp_id),
                    "dataSource": "加班",
                    "status": ot_status,
                    "hrStatus": r.hr_status,
                    "chiefStatus": r.chief_status,
                    "chiefStatusDisplay": r.chief_status,
                    "pendingMakeup": False,
                    "hasScan": True,
                    "laoqinCount": int(laoqin_n),
                    "laoqinWorkHours": recon_now.get("laoqinWorkHours"),
                    "attendanceResultWorkHours": attendance_result.cqgs if attendance_result else None,
                    "valid": ot_valid,
                    "validReason": ot_reason,
                    "validKind": ot_valid_kind,
                    "post": ot.post,
                    "squad": ot.squad,
                    "shiftSlot": ot.shift_slot,
                }
            )
    if restrict_furnace_ids is not None:
        # 当前出勤工作点或固定委托工作点任一归本人即可看见；只有固定/解析出的
        # 委托工作点归本人时才能写值班长确认，避免“看得见但点确认 403”。
        visible_rows = []
        for item in rows:
            review_furnace_id = item.get("delegateFurnaceId")
            item["chiefCanConfirm"] = review_furnace_id in restrict_furnace_ids
            delegated_recon_visible = (
                item.get("rowKind") == "recon"
                and review_furnace_id in restrict_furnace_ids
            )
            if (
                item.get("furnaceId") in restrict_furnace_ids
                or delegated_recon_visible
            ):
                visible_rows.append(item)
        rows = visible_rows

    f = filters or {}
    # scoped_region 已在函数开头解析（行构造时按地区裁剪字段时同源）。

    def _row_regions(row: dict) -> list[str]:
        regions = [p.get("region") for p in row.get("furnaces", []) if p.get("region")]
        if not regions and row.get("furnaceId") is not None:
            fobj = furnaces_by_id.get(row["furnaceId"])
            if fobj and fobj.region:
                row["furnaces"] = [{"region": fobj.region, "furnace": fobj.furnace_no}]
                return [fobj.region]
        return regions

    paired = [(row, _row_regions(row)) for row in rows]
    for row, regions in paired:
        # 只有连 furnaceId 也解析不到地区才算「待归属」；仅有加班记录的行 furnaces 为空
        # 但 furnaceId 可反查地区，不应被当成待归属而跨地区重复出现。
        row["regionPending"] = not regions
    if scoped_region:
        # 无工作点归属通常正是“仅劳勤有打卡/工作点缺失”等待人工处理异常。
        # 地区过滤时保留并显式标成待归属，避免 HR/统计最需要处理的漏单。
        rows = [
            row for row, regions in paired
            if not regions or scoped_region in regions
        ]

    # 筛选项（来自受限后的全量，保证下拉稳定）
    options = {
        "regions": [scoped_region] if scoped_region else sorted({
            pair["region"]
            for x in rows
            for pair in x.get("furnaces", [])
            if pair.get("region")
        }),
        "furnaces": sorted({
            pair["furnace"]
            for x in rows
            for pair in x.get("furnaces", [])
            if pair.get("furnace") and (
                not scoped_region or pair.get("region") == scoped_region
            )
        }),
        "chiefs": sorted({c["name"] for x in rows for c in x.get("furnaceChiefs", []) if c.get("name")}),
        "statuses": list(RECON_STATUSES),
        "confirmStates": list(CONFIRM_STATES),
        "hrConfirmStates": list(HR_CONFIRM_STATES),
        "chiefConfirmStates": list(CHIEF_CONFIRM_STATES),
        "dataSources": ["当天扫码", "本人补录", "值班长代签", "加班"],
        "scanData": ["有", "无"],
        "laoqinBuckets": ["无", "仅一个", "两个及以上"],
        "validKinds": ["班次有效", "班次无效", "自动有效", "自动无效", "手动有效", "手动无效"],
    }
    # 待处理 = 自动无效（系统判无效且尚无人工处理，需 HR/值班长 介入）
    pending_count = sum(1 for x in rows if x["validKind"] == "自动无效")

    def _laoqin_bucket(n: int) -> str:
        return "无" if n == 0 else "仅一个" if n == 1 else "两个及以上"

    def _match(x: dict) -> bool:
        if f.get("furnace"):
            if not any(
                pair.get("furnace") == f["furnace"]
                and (not scoped_region or pair.get("region") == scoped_region)
                for pair in x.get("furnaces", [])
            ):
                return False
        if f.get("name") and f["name"] not in (x["name"] or ""):
            return False
        if f.get("chiefName"):
            chief_names = [c.get("name", "") for c in x.get("furnaceChiefs", [])] or [x.get("chiefName") or ""]
            if not any(f["chiefName"] in (cn or "") for cn in chief_names):
                return False
        if f.get("status") and x["status"] != f["status"]:
            return False
        if f.get("dataSource") and x["dataSource"] != f["dataSource"]:
            return False
        if f.get("hrStatus") and x["hrStatus"] != f["hrStatus"]:
            return False
        if f.get("chiefStatus") and x["chiefStatus"] != f["chiefStatus"]:
            return False
        if f.get("scanData") and (("有" if x["hasScan"] else "无") != f["scanData"]):
            return False
        if f.get("laoqin") and _laoqin_bucket(x["laoqinCount"]) != f["laoqin"]:
            return False
        if f.get("validKind"):
            vk = f["validKind"]
            if vk == "班次有效" and not x["valid"]:
                return False
            if vk == "班次无效" and x["valid"]:
                return False
            if vk in ("自动有效", "自动无效", "手动有效", "手动无效") and x["validKind"] != vk:
                return False
        return True

    items = [x for x in rows if _match(x)]
    # 全量含多日 → 先按日期倒序，再工作点/状态/工号
    items.sort(
        key=lambda x: (
            x["date"] or "",
            x["furnace"],
            x["empId"],
            0 if x.get("rowKind") == "recon" else 1,
            x.get("overtimeId") or 0,
        ),
        reverse=False,
    )
    items.sort(key=lambda x: x["date"] or "", reverse=True)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 50), 200))
    start = (page - 1) * page_size
    page_items = items[start:start + page_size] if paginate else items

    return {
        "date": date,
        "total": len(rows),
        "count": len(items),
        "page": page,
        "pageSize": page_size,
        "pendingCount": pending_count,
        "items": page_items,
        "options": options,
    }


def _chief_emp_for_furnace_id(db: Session, furnace_id: int | None) -> str | None:
    if furnace_id is None:
        return None
    return db.scalar(
        select(FurnaceChief.emp_id)
        .join(Employee, Employee.emp_id == FurnaceChief.emp_id)
        .where(
            FurnaceChief.furnace_id == furnace_id,
            FurnaceChief.deleted.is_(False),
            Employee.deleted.is_(False),
            Employee.employment_status != "departed",
        )
        .order_by(FurnaceChief.id)
    )


def notify_chief_review(db: Session, *, chief_emp_id: str, subject: str, body: str) -> tuple[bool, str]:
    """给指定值班长（chief_emp_id）推送待确认信息，并落库一条记录。返回 (是否成功, 提示)。

    chief_emp_id 即接收推送的值班长工号——来自调用方 set_confirm：
    「该工人扫码工作点的值班长」，若该工作点无值班长则为 HR 在弹窗指定并绑定的值班长。
    推送目标为该值班长的钉钉号(dd_user_id)。
    钉钉凭据已配置 → 发真实钉钉工作通知，并查 getsendresult；不在可见范围/被禁止(143106)
    或无效用户视为失败。未配置 → 仅落库站内通知（值班长 H5「我的通知」可见）。
    无钉钉号 / 发送失败 → 返回失败（前端据此弹二次确认）。"""
    from app.models import NotificationRecord
    from app.services.dingtalk_service import is_configured, send_work_message_sync

    chief = db.scalar(select(Employee).where(Employee.emp_id == chief_emp_id))
    target = (chief.dd_user_id or "").strip() if chief else ""

    def _record(channel: str, status: str, extra: dict) -> None:
        db.add(
            NotificationRecord(
                channel=channel,
                target=chief_emp_id,
                payload=json.dumps(
                    {"subject": subject, "body": body, "ddUserId": target, **extra},
                    ensure_ascii=False,
                ),
                status=status,
                kind="chief_review",
            )
        )
        db.commit()

    if not target:
        _record("none", "skipped", {"reason": "值班长无钉钉号"})
        return False, "值班长无钉钉号，推送失败"

    if not is_configured():
        # 未配置钉钉凭据：仅站内通知（H5「我的通知」可见）
        _record("mock", "sent", {})
        return True, f"已记录站内通知（值班长 {chief_emp_id}）"

    # 真实钉钉工作通知（含 HR 发起人/证据链）+ 查投递结果
    res = send_work_message_sync(target, body)
    if res.get("ok"):
        _record("dingtalk", "sent", {"taskId": res.get("taskId")})
        return True, f"已推送值班长 {chief_emp_id}"
    _record("dingtalk", "skipped", {"reason": res.get("error", "投递失败"), "forbidden": res.get("forbidden", [])})
    return False, res.get("error") or "推送失败"


def _validate_chief_for_furnace(
    db: Session, *, chief_emp_id: str, furnace: Furnace
) -> None:
    chief = db.scalar(
        select(Employee).where(Employee.emp_id == chief_emp_id)
    )
    if chief is None or chief.deleted:
        raise ValueError("指定员工不存在或已停用，请在管理员后台「员工管理」中检查")
    if chief.employment_status == "departed":
        raise ValueError("指定员工已离职，请选择在职值班长")
    # 存量钉钉导入数据可能 role=NULL，但已存在的有效工作点绑定是更强的值班长身份
    # 证据；不单独依赖工号前缀推断，避免给普通员工凭空新增管辖权。
    has_active_chief_binding = db.scalar(
        select(FurnaceChief.id).where(
            FurnaceChief.emp_id == chief_emp_id,
            FurnaceChief.deleted.is_(False),
        )
    ) is not None
    if chief.role != "chief" and not has_active_chief_binding:
        raise ValueError(
            "指定员工的系统角色不是值班长，请先在管理员后台「员工管理」中设为值班长"
        )
    favorite_regions = set(
        db.scalars(
            select(EmployeeFavorite.value)
            .join(Region, Region.name == EmployeeFavorite.value)
            .where(
                EmployeeFavorite.emp_id == chief_emp_id,
                EmployeeFavorite.kind == "region",
                Region.deleted.is_(False),
            )
        ).all()
    )
    bound_regions = set(
        db.scalars(
            select(Furnace.region)
            .join(FurnaceChief, FurnaceChief.furnace_id == Furnace.id)
            .join(Region, Region.name == Furnace.region)
            .where(
                FurnaceChief.emp_id == chief_emp_id,
                FurnaceChief.deleted.is_(False),
                Region.deleted.is_(False),
            )
        ).all()
    )
    known_regions = favorite_regions | bound_regions
    if not known_regions:
        raise ValueError("该值班长尚未配置所属地区，请先在员工管理中设置地区后再委托")
    if furnace.region not in known_regions:
        raise ValueError(f"指定值班长不属于工作点地区：{furnace.region}")


def set_confirm(
    db: Session,
    *,
    emp_id: str,
    date: str,
    kind: str,
    status: str,
    actor: str,
    chief_emp_id: str | None = None,
    furnace_id: int | None = None,
) -> dict:
    """HR/值班长 逐条确认。两者相互独立。
    HR 第四态「待值班长确认」：委托值班长——会给该工作点值班长推送待确认信息；
    若该工作点无值班长，前端须传 chief_emp_id 指定同地区、在职且显式角色为 chief
    或已有有效工作点绑定的员工（绑定为该工作点值班长）后再推送。
    返回含 chiefNotified / needChief / chiefHint 供前端决定二次确认。"""
    date = resolve_demo_date(date)
    if kind == "hr":
        if status not in HR_CONFIRM_STATES:
            raise ValueError(f"hr status must be one of {HR_CONFIRM_STATES}")
    elif kind == "chief":
        if status not in CHIEF_CONFIRM_STATES:
            raise ValueError(f"chief status must be one of {CHIEF_CONFIRM_STATES}")
    else:
        raise ValueError("kind must be hr or chief")
    row = db.scalar(
        select(ReconResult).where(ReconResult.emp_id == emp_id, ReconResult.date == date)
    )
    if not row:
        # Read-only lists no longer create rows. Materialize only this employee/day
        # in the explicit confirmation transaction; a concurrent creator may win.
        try:
            with db.begin_nested():
                run_recon_for_date(db, date, commit=False, employee_ids={emp_id})
                db.flush()
        except IntegrityError:
            pass
        row = db.scalar(select(ReconResult).where(
            ReconResult.emp_id == emp_id, ReconResult.date == date,
        ))
    if not row:
        raise ValueError("recon record not found")
    delegate_furnace = None
    chief_id: str | None = None
    chief_to_bind: str | None = None
    if kind == "hr" and status == "待值班长确认":
        furnace_id = furnace_id or _furnace_id_for_emp(db, emp_id, date)
        delegate_furnace = (
            db.scalar(
                select(Furnace)
                .where(Furnace.id == furnace_id)
                .with_for_update()
            )
            if furnace_id is not None
            else None
        )
        if delegate_furnace is None:
            raise ValueError("无法定位该员工当日工作点，请先修复考勤归属后再委托值班长")
        existing_chief_id = _chief_emp_for_furnace_id(db, furnace_id)
        if existing_chief_id:
            # 前端陈旧的“缺值班长”状态可能仍携带 chief_emp_id；已有在职值班长时
            # 一律沿用现有绑定，不让一次错误选择扩张工作点管辖权。
            chief_id = existing_chief_id
        elif chief_emp_id:
            _validate_chief_for_furnace(
                db, chief_emp_id=chief_emp_id, furnace=delegate_furnace
            )
            chief_id = chief_emp_id
            chief_to_bind = chief_emp_id
        if not chief_id:
            # 在获取写锁前返回，保留前端二次选择协议且不产生无人处理的待办。
            return {
                "ok": False,
                "empId": emp_id,
                "date": date,
                "kind": kind,
                "status": status,
                "chiefNotified": False,
                "needChief": True,
                "chiefHint": "该工作点未绑定在职值班长，请指定一名值班长",
                "currentStatus": row.hr_status,
            }
    # 所有可能早退的业务校验完成后再锁定待写行。
    row = db.scalar(
        select(ReconResult)
        .where(ReconResult.emp_id == emp_id, ReconResult.date == date)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not row:  # 极少数并发删除保护
        raise ValueError("recon record not found")
    delegate_furnace_before = row.delegate_furnace_id
    now = datetime.now(timezone.utc)
    if kind == "hr":
        row.hr_status, row.hr_by, row.hr_at = status, actor, now
        row.delegate_furnace_id = furnace_id if status == "待值班长确认" else None
    else:
        row.chief_status, row.chief_by, row.chief_at = status, actor, now
        if row.hr_status == "待值班长确认" and furnace_id is not None:
            # 固化委托已因跨地区证据失效而回落时，把当前合法目标同步落库，
            # 使后续审计与权限解析不再反复依赖旧的跨地区值。
            row.delegate_furnace_id = furnace_id
    db.add(
        AuditLog(
            actor=actor,
            action=f"anomaly_confirm_{kind}",
            payload=json.dumps(
                {
                    "empId": emp_id,
                    "date": date,
                    "status": status,
                    "delegateFurnaceIdBefore": delegate_furnace_before,
                    "delegateFurnaceIdAfter": row.delegate_furnace_id,
                },
                ensure_ascii=False,
            ),
        )
    )
    if kind == "hr" and status == "待值班长确认" and chief_to_bind:
        # 绑定与确认状态共用一次提交；upsert 消除并发唯一键冲突。
        _bind_chief(db, furnace_id, chief_to_bind, actor)
    db.commit()

    out = {"ok": True, "empId": emp_id, "date": date, "kind": kind, "status": status}
    # HR 委托值班长 → 推送给「该工人扫码工作点的值班长」（chief_id）；该工作点无值班长则用 HR 指定的值班长
    if kind == "hr" and status == "待值班长确认":
        # 委托的业务路由有意按「当日最近有效扫码工作点」确定，并与 chief/m2
        # 待办队列使用同一个解析函数；不能复用路由层仅用于 HR 地区权限校验的
        # 回退工作点，否则会出现通知给本地区值班长、待办却只对外地区值班长可见。
        furnace_no = delegate_furnace.furnace_no
        # chief_id 已在事务提交前解析；此时必定存在。
        name = db.scalar(select(Employee.name).where(Employee.emp_id == emp_id)) or emp_id
        hr_name = db.scalar(select(Employee.name).where(Employee.emp_id == actor)) or actor
        body = _build_delegate_message(
            db, emp_id=emp_id, name=name, date=date, furnace_no=furnace_no,
            status=row.status, hr_actor=actor, hr_name=hr_name,
        )
        ok, hint = notify_chief_review(
            db, chief_emp_id=chief_id, subject="【三班倒打卡】待值班长确认", body=body
        )
        out.update({"chiefNotified": ok, "needChief": False, "chiefHint": hint})
    return out


def _build_delegate_message(
    db: Session, *, emp_id: str, name: str, date: str, furnace_no: str,
    status: str, hr_actor: str, hr_name: str,
) -> str:
    """HR 转交「待值班长确认」推送给值班长的钉钉正文：发起人HR + 证据链 + 去审核指引。"""
    from app.services.anomaly_evidence import get_anomaly_evidence

    ev = get_anomaly_evidence(db, emp_id=emp_id, date=date)
    lines = [
        "【三班倒打卡】待值班长确认",
        f"发起人(HR)：{hr_name}（{hr_actor}）",
        f"员工：{name}（{emp_id}）",
        f"工作点：{ev.get('region', '')}·{furnace_no}",
        f"对账状态：{status}",
    ]
    scans = ev.get("scanRecords") or []
    if scans:
        s = scans[0]
        half = " 半班" if (s.get("workHours") or 8) < 8 else ""
        lines.append(
            f"扫码：{(s.get('time') or '')[11:19]} {s.get('squad') or '不分班'} "
            f"{s.get('post')}·{s.get('employType')}{half}·来源{s.get('source')}"
        )
    punches = ev.get("laoqinPunches") or []
    if punches:
        lines.append(
            "钉钉打卡：" + "、".join(f"{(p.get('time') or '')[11:19]}{p.get('inoutType')}" for p in punches)
        )
    if ev.get("attendanceResultWorkHours"):
        lines.append(f"出勤工时：{ev.get('attendanceResultWorkHours')}小时")
    makeups = ev.get("makeups") or []
    if makeups:
        m = makeups[0]
        lines.append(f"补录/代签：{m.get('kind')}·{m.get('status')}·{m.get('post')}")
    lines.append("请进入 H5「值班长审核确认」处理该条记录。")
    return "\n".join(lines)


def _bind_chief(
    db: Session, furnace_id: int | None, chief_emp_id: str, actor: str
) -> None:
    if furnace_id is None or db.get(Furnace, furnace_id) is None:
        return
    binding = db.scalar(
        select(FurnaceChief).where(
            FurnaceChief.furnace_id == furnace_id,
            FurnaceChief.emp_id == chief_emp_id,
        )
    )
    if binding and not binding.deleted:
        return
    operation = "restore" if binding else "create"
    values = {
        "furnace_id": furnace_id,
        "emp_id": chief_emp_id,
        "created_by": actor,
        "updated_by": actor,
        "deleted": False,
    }
    update_values = {
        "deleted": False,
        "updated_by": actor,
        "updated_at": datetime.now(timezone.utc),
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - 生产与测试分别是 PostgreSQL / SQLite
        raise RuntimeError(f"不支持的数据库方言：{dialect}")
    stmt = insert(FurnaceChief).values(**values)
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[FurnaceChief.furnace_id, FurnaceChief.emp_id],
            set_=update_values,
        )
    )
    db.add(
        AuditLog(
            actor=actor,
            action="furnace_chief_bind",
            payload=json.dumps(
                {
                    "furnaceId": furnace_id,
                    "chiefEmpId": chief_emp_id,
                    "operation": operation,
                    "source": "hr_delegate",
                },
                ensure_ascii=False,
            ),
        )
    )


def get_chief_review_queue(
    db: Session, *, chief_emp_id: str, date: str | None = None
) -> dict:
    """值班长 H5 审核确认页数据：本人所辖工作点的
    ① 待审本人补录（值班长审批通过→生成扫码，与扫码同权）
    ② HR 转交「待值班长确认」且值班长尚未确认的对账行。

    date 为空 → 返回全部日期（前端默认不选日期、看所有待办，自行翻页）；
    给定 date → 仅该日。每条带各自的 natual_date，便于跨日列表展示。
    """
    date = resolve_demo_date(date) if date else None
    f_ids = db.scalars(
        select(FurnaceChief.furnace_id).where(
            FurnaceChief.emp_id == chief_emp_id, FurnaceChief.deleted.is_(False)
        )
    ).all()
    furnaces = {
        fid: furnace
        for fid in f_ids
        if (furnace := db.get(Furnace, fid)) is not None
    }
    emp_name = {e.emp_id: e.name for e in db.scalars(select(Employee)).all()}

    makeup_q = select(AttendanceRecord).where(
        AttendanceRecord.status == "待审",
        AttendanceRecord.source == "补录",
        AttendanceRecord.furnace_id.in_(list(furnaces) or [-1]),
    )
    if date:
        makeup_q = makeup_q.where(AttendanceRecord.natual_date == date)
    makeup_items = []
    for m in db.scalars(makeup_q.order_by(AttendanceRecord.natual_date.desc())).all():
        f = furnaces.get(m.furnace_id)
        if f is None:
            continue
        makeup_items.append(
            {
                "kind": "makeup",
                "empId": m.emp_id,
                "name": emp_name.get(m.emp_id, m.emp_id),
                "date": m.natual_date,
                "region": f.region,
                "furnace": f.furnace_no,
                "squad": m.squad,
                "post": m.post,
                "employType": m.employ_type,
                "reason": m.reason,
            }
        )

    delegated_q = select(ReconResult).where(
        ReconResult.hr_status == "待值班长确认",
        ReconResult.chief_status == "未确认",
    )
    if date:
        delegated_q = delegated_q.where(ReconResult.date == date)
    delegated_items = []
    for r in db.scalars(delegated_q.order_by(ReconResult.date.desc())).all():
        furnace_id = resolve_review_furnace_id(db, r.emp_id, r.date, r)
        if furnace_id not in furnaces:
            continue
        furnace = furnaces[furnace_id]
        delegated_items.append(
            {
                "kind": "delegated",
                "empId": r.emp_id,
                "name": emp_name.get(r.emp_id, r.emp_id),
                "date": r.date,
                "furnace": furnace.furnace_no,
                "status": r.status,
                "dataSource": _data_source(db, r.emp_id, r.date),
            }
        )
    return {
        "date": date,
        "makeups": makeup_items,
        "delegated": delegated_items,
        "count": len(makeup_items) + len(delegated_items),
    }


def get_m2_review_queue(db: Session, *, furnace_id: int, date: str) -> list[dict]:
    """本工作点「委托待值班长确认」队列：HR 判「待值班长确认」且值班长未确认的记录。
    供日确认页展示，与值班长审核页(ChiefReview)的 delegated 口径一致，避免只看日确认页
    的值班长漏办。按 furnace_id 精确过滤(跨地区同名工作点不混入)。"""
    emp_name = {e.emp_id: e.name for e in db.scalars(select(Employee)).all()}
    out: list[dict] = []
    for r in db.scalars(
        select(ReconResult).where(
            ReconResult.date == date,
            ReconResult.hr_status == "待值班长确认",
            ReconResult.chief_status == "未确认",
        )
    ).all():
        delegated_furnace_id = resolve_review_furnace_id(db, r.emp_id, date, r)
        if delegated_furnace_id != furnace_id:
            continue
        out.append(
            {
                "kind": "delegated",
                "empId": r.emp_id,
                "name": emp_name.get(r.emp_id, r.emp_id),
                "date": date,
                "status": r.status,
                "dataSource": _data_source(db, r.emp_id, date),
            }
        )
    return out
