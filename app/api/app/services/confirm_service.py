import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AttendanceRecord,
    AuditLog,
    ChiefConfirm,
    ChiefWorker,
    Employee,
    FurnaceChief,
)
from app.errors import ConflictError, DomainError, NotFoundError
from app.scan_active import (
    ATTENDANCE_KIND_OVERTIME,
    ATTENDANCE_KIND_WORK,
    SCAN_STATUS_CANCELLED,
    STANDARD_SHIFT_HOURS,
    scan_is_active,
)
from app.services.furnace_lookup import get_furnace
from app.services.anomaly_service import get_m2_review_queue
from app.services.monthly_export_lock_service import assert_month_export_unlocked
from app.services.recon_service import run_recon_for_date

MAX_DAILY_WORK_HOURS = 24.0


def _assert_employee_day_export_unlocked(
    db: Session, *, emp_id: str, date: str, fallback_furnace_id: int
) -> None:
    """写路径会重算整条员工日对账，月锁必须覆盖当日全部工作点。"""
    from app.services.anomaly_service import furnace_ids_for_emp_on_date

    fids = furnace_ids_for_emp_on_date(db, emp_id, date) or [fallback_furnace_id]
    for fid in fids:
        assert_month_export_unlocked(db, date, fid)


def _parse_exclusions(detail: str | None) -> set[str]:
    return set(_parse_detail_json(detail).get("excluded", []))


def daily_work_hours_used(
    db: Session,
    *,
    emp_id: str,
    date: str,
    exclude_record_ids: set[int] | None = None,
) -> float:
    exclude_record_ids = exclude_record_ids or set()
    records = db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.status == "active",
        )
    ).all()
    return round(
        sum(
            (r.work_hours if r.work_hours is not None else STANDARD_SHIFT_HOURS)
            for r in records
            if r.id not in exclude_record_ids
        ),
        2,
    )


def assert_daily_work_hours_limit(
    db: Session,
    *,
    emp_id: str,
    date: str,
    incoming_hours: float,
    exclude_record_ids: set[int] | None = None,
) -> float:
    used = daily_work_hours_used(
        db, emp_id=emp_id, date=date, exclude_record_ids=exclude_record_ids
    )
    remaining = round(MAX_DAILY_WORK_HOURS - used, 2)
    if incoming_hours > remaining:
        raise DomainError(
            f"该员工当天已使用 {used:g}h，最多还可使用 {max(0, remaining):g}h，实际工作时长不能超过 24h"
        )
    return remaining


def get_confirm_sections(
    db: Session,
    *,
    furnace_no: str,
    date: str,
    region: str | None = None,
    include_departed_candidates: bool = True,
) -> dict:
    furnace = get_furnace(db, furnace_no, region)
    if not furnace:
        return {
            "scanned": [],
            "notScanned": [],
            "overtimeCandidates": [],
            "overtime": [],
            "external": [],
            "pendingMakeup": [],
            "reviewQueue": [],
            "shortage": [],
            "locked": False,
            "confirmedBy": None,
        }

    confirm = db.scalar(
        select(ChiefConfirm).where(
            ChiefConfirm.furnace_id == furnace.id, ChiefConfirm.natual_date == date
        )
    )
    exclusions = _parse_exclusions(confirm.detail if confirm else None)

    scans = db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.furnace_id == furnace.id,
            AttendanceRecord.natual_date == date,
            scan_is_active(),
        )
    ).all()
    scanned_emp_ids = {s.emp_id for s in scans}

    # 值班长-常用员工映射：本工作点「全部值班长」的常用名单 + 全量已绑定名单（用于「外来」标记/绑定按钮）。
    # 多值班长表(furnace_chief)优先，兼容旧 chief_user_id 列——否则仅经多值班长表绑定的工作点
    # (chief_user_id 为 NULL) 会把所有正常扫码工人误判为「外来」、「已扫(本工作点)」面板恒空。
    furnace_chief_ids = [
        fc.emp_id
        for fc in db.scalars(
            select(FurnaceChief).where(
                FurnaceChief.furnace_id == furnace.id, FurnaceChief.deleted.is_(False)
            )
        )
    ]
    if not furnace_chief_ids and furnace.chief_user_id:
        furnace_chief_ids = [furnace.chief_user_id]
    chief_worker_rows = (
        db.execute(
            select(ChiefWorker.emp_id, Employee.employment_status)
            .join(Employee, Employee.emp_id == ChiefWorker.emp_id)
            .where(
                ChiefWorker.chief_user_id.in_(furnace_chief_ids),
                Employee.deleted.is_(False),
            )
        ).all()
        if furnace_chief_ids
        else []
    )
    all_chief_workers = {emp_id for emp_id, _ in chief_worker_rows}
    chief_workers = {
        emp_id
        for emp_id, employment_status in chief_worker_rows
        if include_departed_candidates or employment_status == "active"
    }
    any_bound = set(
        db.scalars(
            select(ChiefWorker.emp_id)
            .join(Employee, Employee.emp_id == ChiefWorker.emp_id)
            .where(Employee.deleted.is_(False))
        )
    )
    # 值班长本人视为本工作点「常用」：本人扫码不判「外来」、也不显示「绑定」按钮；
    # 但不并入 chief_workers（否则值班长未扫码会错列入「未扫」名单）。
    furnace_chief_set = set(furnace_chief_ids)

    def _bound(emp_id: str) -> bool:
        return emp_id in all_chief_workers or emp_id in furnace_chief_set

    def _bound_any(emp_id: str) -> bool:
        return emp_id in any_bound or emp_id in furnace_chief_set

    scanned = []
    for s in scans:
        if s.emp_id in exclusions:
            continue
        emp = db.scalar(select(Employee).where(Employee.emp_id == s.emp_id))
        scanned.append(
            {
                "empId": s.emp_id,
                "name": emp.name if emp else s.emp_id,
                "post": s.post,
                "squad": s.squad,
                "employType": s.employ_type,
                "workHours": s.work_hours if s.work_hours is not None else STANDARD_SHIFT_HOURS,
                "excluded": False,
                "boundToChief": _bound(s.emp_id),
                "boundToAnyChief": _bound_any(s.emp_id),
            }
        )

    for emp_id in exclusions:
        emp = db.scalar(select(Employee).where(Employee.emp_id == emp_id))
        voided = db.scalar(
            select(AttendanceRecord).where(
                AttendanceRecord.emp_id == emp_id,
                AttendanceRecord.furnace_id == furnace.id,
                AttendanceRecord.natual_date == date,
                AttendanceRecord.record_kind == ATTENDANCE_KIND_WORK,
                AttendanceRecord.status == SCAN_STATUS_CANCELLED,
            )
        )
        scanned.append(
            {
                "empId": emp_id,
                "name": emp.name if emp else emp_id,
                "post": voided.post if voided else "—",
                "squad": voided.squad if voided else None,
                "employType": voided.employ_type if voided else "—",
                "workHours": voided.work_hours if voided and voided.work_hours is not None else STANDARD_SHIFT_HOURS,
                "excluded": True,
                "boundToChief": _bound(emp_id),
                "boundToAnyChief": _bound_any(emp_id),
            }
        )

    pending = db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.furnace_id == furnace.id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.status == "待审",
        )
    ).all()
    pending_makeup = []
    for m in pending:
        emp = db.scalar(select(Employee).where(Employee.emp_id == m.emp_id))
        pending_makeup.append(
            {
                "id": m.id,
                "empId": m.emp_id,
                "name": emp.name if emp else m.emp_id,
                "post": m.post,
                "squad": m.squad,
                "reason": m.reason,
            }
        )

    # 当天已有任意记录(active 出勤/加班 或 待审补录，跨工作点)的人 → 代签候选要排除、加班候选要纳入
    employees = db.scalars(select(Employee).where(Employee.deleted.is_(False))).all()
    emp_names = {employee.emp_id: employee.name for employee in employees}
    employment_statuses = {
        employee.emp_id: employee.employment_status for employee in employees
    }
    has_record_ids: set[str] = (
        set(
            db.scalars(
                select(AttendanceRecord.emp_id).where(
                    AttendanceRecord.emp_id.in_(chief_workers),
                    AttendanceRecord.natual_date == date,
                    AttendanceRecord.status.in_(["active", "待审"]),
                )
            )
        )
        if chief_workers
        else set()
    )
    # 未扫(代签候选)：本工作点值班长「常用员工」当天无任何记录的人
    not_scanned = [
        {
            "empId": emp_id,
            "name": emp_names.get(emp_id, emp_id),
            "employmentStatus": employment_statuses.get(emp_id, "active"),
        }
        for emp_id in sorted(chief_workers)
        if emp_id not in has_record_ids and emp_id not in exclusions
    ]
    # 加班候选：当天已有 active 记录(本工作点常用员工 ∪ 本工作点已扫)的人，带其已占班次(供前端置灰)
    candidate_ids = set(chief_workers) | scanned_emp_ids
    if not include_departed_candidates:
        candidate_ids = {
            emp_id
            for emp_id in candidate_ids
            if employment_statuses.get(emp_id, "active") == "active"
        }
    overtime_candidates: list[dict] = []
    if candidate_ids:
        ot_by_emp: dict[str, set[str]] = {}
        slots_by_emp: dict[str, set[str]] = {}
        for r in db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.emp_id.in_(candidate_ids),
                AttendanceRecord.natual_date == date,
                AttendanceRecord.status == "active",
            )
        ):
            ot_by_emp.setdefault(r.emp_id, set()).add(r.squad or "不分班")
            if r.record_kind == ATTENDANCE_KIND_OVERTIME and r.shift_slot:
                slots_by_emp.setdefault(r.emp_id, set()).add(r.shift_slot)
        for eid, squads in sorted(ot_by_emp.items()):
            used_hours = daily_work_hours_used(db, emp_id=eid, date=date)
            overtime_candidates.append(
                {
                    "empId": eid,
                    "name": emp_names.get(eid, eid),
                    "employmentStatus": employment_statuses.get(eid, "active"),
                    "squads": sorted(squads),
                    "slots": sorted(slots_by_emp.get(eid, set())),
                    "usedHours": used_hours,
                    "remainingHours": max(0.0, round(MAX_DAILY_WORK_HOURS - used_hours, 2)),
                }
            )
    # 外来：扫了码但不在「常用员工」里的人（不计入已扫）
    external = [
        {
            "empId": s["empId"],
            "name": s["name"],
            "post": s["post"],
            "squad": s["squad"],
            "employType": s["employType"],
            "boundToChief": s["boundToChief"],
            "boundToAnyChief": s["boundToAnyChief"],
        }
        for s in scanned
        if not s.get("boundToChief") and not s.get("excluded")
    ]

    # 本工作点本日的加班记录（record_kind=加班）：单列展示，供日结页「当前编制/加班」可见，
    # 也让跨工作点加班（工人基础出勤在其他工作点）的发起值班长能在本工作点看到自己加的班。
    overtime = []
    for r in db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.furnace_id == furnace.id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.status == "active",
            AttendanceRecord.record_kind == ATTENDANCE_KIND_OVERTIME,
        )
    ):
        emp = db.scalar(select(Employee).where(Employee.emp_id == r.emp_id))
        overtime.append(
            {
                "id": r.id,
                "empId": r.emp_id,
                "name": emp.name if emp else r.emp_id,
                "post": r.post,
                "squad": r.squad,
                "shiftSlot": r.shift_slot,
                "employType": r.employ_type,
                "workHours": r.work_hours if r.work_hours is not None else STANDARD_SHIFT_HOURS,
                "reconDingtalk": r.recon_dingtalk,
            }
        )

    review_queue = get_m2_review_queue(db, furnace_id=furnace.id, date=date)

    from app.services.staffing_service import compute_shortage

    shortage = compute_shortage(db, furnace_no=furnace_no, date=date, region=region)

    # 本工作点所有值班长（一工作点多值班长）：日确认页展示"自己和其他值班长"
    fc_emp_ids = [
        fc.emp_id
        for fc in db.scalars(select(FurnaceChief).where(FurnaceChief.furnace_id == furnace.id))
    ]
    if not fc_emp_ids and furnace.chief_user_id:
        fc_emp_ids = [furnace.chief_user_id]
    name_by = {e.emp_id: e.name for e in db.scalars(select(Employee).where(Employee.emp_id.in_(fc_emp_ids)))} if fc_emp_ids else {}
    chiefs = [{"empId": eid, "name": name_by.get(eid, eid)} for eid in fc_emp_ids]

    return {
        "scanned": scanned,
        "notScanned": not_scanned,
        "overtimeCandidates": overtime_candidates,
        "overtime": overtime,
        "external": external,
        "pendingMakeup": pending_makeup,
        "reviewQueue": review_queue,
        "shortage": shortage,
        "chiefs": chiefs,
        "locked": bool(confirm and confirm.locked),
        "confirmedBy": confirm.confirmed_by if confirm else None,
    }


def _get_or_create_confirm(db: Session, furnace_id: int, date: str) -> ChiefConfirm:
    row = db.scalar(
        select(ChiefConfirm).where(
            ChiefConfirm.furnace_id == furnace_id, ChiefConfirm.natual_date == date
        )
    )
    if row:
        return row
    row = ChiefConfirm(furnace_id=furnace_id, natual_date=date, locked=False)
    db.add(row)
    db.flush()
    return row


def _assert_not_locked(db: Session, furnace_id: int, date: str) -> None:
    """本日已锁定则禁止改动（撤回/标半天/调班/代签）——否则算薪基准锁不住、可被悄改。"""
    row = db.scalar(
        select(ChiefConfirm).where(
            ChiefConfirm.furnace_id == furnace_id, ChiefConfirm.natual_date == date
        )
    )
    if row and row.locked:
        raise ConflictError("本日已锁定确认，不能再修改；如需修改请先解锁")


def toggle_confirm_exclusion(
    db: Session,
    *,
    furnace_no: str,
    date: str,
    emp_id: str,
    excluded: bool,
    region: str | None = None,
    reason: str | None = None,
    actor: str | None = None,
) -> dict:
    from app.services.anomaly_service import resolve_demo_date

    natual_date = resolve_demo_date(date)
    furnace = get_furnace(db, furnace_no, region)
    if not furnace:
        raise NotFoundError("furnace not found")
    _assert_employee_day_export_unlocked(
        db, emp_id=emp_id, date=natual_date, fallback_furnace_id=furnace.id
    )
    _assert_not_locked(db, furnace.id, natual_date)
    confirm = _get_or_create_confirm(db, furnace.id, natual_date)
    data = _parse_detail_json(confirm.detail)
    excl = set(data.get("excluded", []))
    if excluded:
        excl.add(emp_id)
    else:
        excl.discard(emp_id)
    data["excluded"] = sorted(excl)
    confirm.detail = json.dumps(data, ensure_ascii=False)

    # 出勤记录（含 扫码 / 代签 / 已审补录）：并表后代签即此行，取消/恢复直接改其 status，
    # 无需再单独同步「代签单」。仅作用于 record_kind=出勤，加班记录不受日结取消影响。
    scans = db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.furnace_id == furnace.id,
            AttendanceRecord.natual_date == natual_date,
            AttendanceRecord.record_kind == ATTENDANCE_KIND_WORK,
        )
    ).all()
    if not excluded:
        # 恢复打卡守卫：当天该工人若已有可用(active)打卡，则撤回记录不可恢复
        other_active = db.scalar(
            select(AttendanceRecord).where(
                AttendanceRecord.emp_id == emp_id,
                AttendanceRecord.natual_date == natual_date,
                scan_is_active(),
            )
        )
        if other_active:
            raise ConflictError("当天已有可用打卡，撤回记录不可恢复")

    if scans:
        if excluded:
            for scan in scans:
                if scan.status == "active":
                    scan.status = SCAN_STATUS_CANCELLED
            db.add(
                AuditLog(
                    actor=actor or "chief",
                    action="confirm_exclude",
                    payload=json.dumps(
                        {"empId": emp_id, "date": natual_date, "reason": reason or ""},
                        ensure_ascii=False,
                    ),
                )
            )
        else:
            for scan in scans:
                if scan.status == SCAN_STATUS_CANCELLED:
                    scan.status = "active"

    db.commit()
    run_recon_for_date(db, natual_date)
    return {"ok": True, "excluded": list(excl)}


def set_work_hours(
    db: Session,
    *,
    furnace_no: str,
    date: str,
    emp_id: str,
    work_hours: float,
    record_id: int | None = None,
    region: str | None = None,
    actor: str | None = None,
) -> dict:
    """值班长在日结侧核定某人当日工时（高自由度）。

    work_hours 取 0~24：0=取消该打卡；4=半班；8=整班；>8=班次延时（如 12h=整班8+延时4，
    单条记录直接存 12）。
    record_id 指定时只改该单条记录（用于编辑/取消某条加班记录，含一天多班次的加班）。
    """
    from app.services.anomaly_service import resolve_demo_date

    if work_hours < 0:
        raise DomainError("work_hours must be >= 0")
    if work_hours > MAX_DAILY_WORK_HOURS:
        raise DomainError("工时不能超过 24h")

    # 班时口径：低于整班(8h)仅可选半班(4h)或撤回(0)；8~24h（整班/班次延时）可填写。
    # 与扫码侧仅 4/8 一致，杜绝 1/2/3/5/6/7 等任意值污染占用汇总与算薪（两个半班合一位）。
    if work_hours != 0 and work_hours != 4.0 and work_hours < 8.0:
        raise DomainError("工时低于 8h 仅可选择 4h（半班）或 0h（撤回），8~24h 可填写")

    natual_date = resolve_demo_date(date)
    furnace = get_furnace(db, furnace_no, region)
    if not furnace:
        raise NotFoundError("furnace not found")
    _assert_employee_day_export_unlocked(
        db, emp_id=emp_id, date=natual_date, fallback_furnace_id=furnace.id
    )
    _assert_not_locked(db, furnace.id, natual_date)

    if record_id is not None:
        # 指定单条记录（出勤或加班）：编辑/取消该条。
        rec = db.scalar(
            select(AttendanceRecord).where(
                AttendanceRecord.id == record_id,
                AttendanceRecord.furnace_id == furnace.id,
                AttendanceRecord.natual_date == natual_date,
                AttendanceRecord.status == "active",
            )
        )
        if not rec:
            raise NotFoundError("record not found")
        is_cancel = work_hours == 0
        if not is_cancel:
            assert_daily_work_hours_limit(
                db,
                emp_id=rec.emp_id,
                date=natual_date,
                incoming_hours=work_hours,
                exclude_record_ids={rec.id},
            )
        if is_cancel:
            rec.status = SCAN_STATUS_CANCELLED
        else:
            rec.work_hours = work_hours
        db.add(
            AuditLog(
                actor=actor or "chief",
                action="confirm_set_hours_cancel" if is_cancel else "confirm_set_hours",
                payload=json.dumps(
                    {
                        "recordId": record_id,
                        "empId": rec.emp_id,
                        "date": natual_date,
                        "recordKind": rec.record_kind,
                        "workHours": work_hours,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        db.flush()
        # 工时变化和取消都会影响待办结论，在同一写入事务刷新本员工日。
        run_recon_for_date(db, natual_date, commit=False, employee_ids={rec.emp_id})
        db.commit()
        return {
            "ok": True,
            "empId": rec.emp_id,
            "workHours": work_hours,
            "cancelled": is_cancel,
        }

    scans = db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.furnace_id == furnace.id,
            AttendanceRecord.natual_date == natual_date,
            scan_is_active(),
        )
    ).all()
    if not scans:
        raise NotFoundError("scan not found")

    # 0 小时 = 取消该打卡（复用「否认此条打卡」语义）
    if work_hours == 0:
        for scan in scans:
            scan.status = SCAN_STATUS_CANCELLED
        db.add(
            AuditLog(
                actor=actor or "chief",
                action="confirm_set_hours_cancel",
                payload=json.dumps(
                    {"empId": emp_id, "date": natual_date}, ensure_ascii=False
                ),
            )
        )
        db.flush()
        run_recon_for_date(db, natual_date, commit=False, employee_ids={emp_id})
        db.commit()
        return {"ok": True, "empId": emp_id, "workHours": 0, "cancelled": True}

    assert_daily_work_hours_limit(
        db,
        emp_id=emp_id,
        date=natual_date,
        incoming_hours=work_hours * len(scans),
        exclude_record_ids={s.id for s in scans},
    )

    for scan in scans:
        scan.work_hours = work_hours
    db.add(
        AuditLog(
            actor=actor or "chief",
            action="confirm_set_hours",
            payload=json.dumps(
                {
                    "empId": emp_id,
                    "date": natual_date,
                    "workHours": work_hours,
                },
                ensure_ascii=False,
            ),
        )
    )
    db.flush()
    run_recon_for_date(db, natual_date, commit=False, employee_ids={emp_id})
    db.commit()
    return {"ok": True, "empId": emp_id, "workHours": work_hours}


def reassign_scan_post(
    db: Session,
    *,
    furnace_no: str,
    date: str,
    emp_id: str,
    post: str,
    squad: str | None = None,
    region: str | None = None,
    actor: str | None = None,
) -> dict:
    """值班长把某人当日在本工作点的扫码打卡调整到其他岗位（可选连带调整班次）。"""
    from app.services.anomaly_service import resolve_demo_date

    from app.services.quota_service import find_quota, resolve_quota_post

    natual_date = resolve_demo_date(date)
    furnace = get_furnace(db, furnace_no, region)
    if not furnace:
        raise NotFoundError("furnace not found")
    _assert_employee_day_export_unlocked(
        db, emp_id=emp_id, date=natual_date, fallback_furnace_id=furnace.id
    )
    _assert_not_locked(db, furnace.id, natual_date)
    scans = db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.furnace_id == furnace.id,
            AttendanceRecord.natual_date == natual_date,
            scan_is_active(),
        )
    ).all()
    if not scans:
        raise NotFoundError("scan not found")
    target_squad = squad if squad is not None else scans[0].squad
    canon = resolve_quota_post(
        db,
        furnace_id=furnace.id,
        region=furnace.region,
        post=post,
        squad=target_squad,
    )

    # 目标岗位必须在该工作点编制规则内
    q = find_quota(
        db,
        region=furnace.region,
        furnace=furnace.furnace_no,
        post=canon,
        squad=target_squad,
        natual_date=natual_date,
    )
    if not q:
        raise NotFoundError("目标岗位不在该工作点编制内")
    # 正式工：目标岗剩余编制需 >= 调整所需编制位（已在目标岗位则不增加占用）
    # 工时 → 编制位：整班8h=1位，半班4h=0.5位。
    formal_need = sum(
        (s.work_hours or STANDARD_SHIFT_HOURS) / STANDARD_SHIFT_HOURS
        for s in scans
        if s.employ_type == "正式"
    )
    already_at_target = all(s.post == canon and s.squad == target_squad for s in scans)
    if formal_need > 0 and not already_at_target:
        remaining = q["capacity"] - q["formalUsed"]
        if remaining < formal_need:
            raise ConflictError(
                f"目标岗位剩余编制不足（剩余 {remaining:g}，需 {formal_need:g}）"
            )

    for scan in scans:
        scan.post = canon
        if squad is not None:
            scan.squad = squad
    db.add(
        AuditLog(
            actor=actor or "chief",
            action="confirm_reassign_post",
            payload=json.dumps(
                {"empId": emp_id, "date": natual_date, "post": canon, "squad": squad},
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    run_recon_for_date(db, natual_date)
    return {"ok": True, "empId": emp_id, "post": canon, "squad": squad}


def _parse_detail_json(detail: str | None) -> dict:
    if not detail:
        return {}
    try:
        return json.loads(detail)
    except json.JSONDecodeError:
        return {}
