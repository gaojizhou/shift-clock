"""考勤对照时间轴：某日全员 扫码 vs 钉钉打卡 时间点对照。

展示层数据（匹配算法后续在此基础上做）：
- 每人一条 item：扫码点（含扫码所选 工作点/班次/岗位/用工类型）+ 钉钉打卡点（透传
  inout_type，上/下班方向仅供参考展示，不参与归类）+ 对账状态/最小时差。
- squadSchedules：各地区早/中/晚班起止时间（squad_schedule 表，HR 维护的班次时间表）。
  end<=start 表示跨午夜，归一为 crossesMidnight 标记。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AttendanceRecord,
    Employee,
    EmployeeFavorite,
    Furnace,
    LaoqinPunch,
    SquadSchedule,
)
from app.domain.timezone import as_shanghai_datetime
from app.scan_active import ATTENDANCE_KIND_OVERTIME, scan_is_active
from app.services.recon_service import (
    get_laoqin_punches_for_recon,
    get_overtime_recon_detail,
    get_recon_for,
)


def get_squad_schedules(db: Session) -> list[dict]:
    by_region: dict[str, list[dict]] = {}
    for ss in db.scalars(
        select(SquadSchedule).where(SquadSchedule.deleted.is_(False)).order_by(SquadSchedule.squad)
    ).all():
        by_region.setdefault(ss.region, []).append(
            {
                "squad": ss.squad,
                "from": ss.start_time,
                "to": ss.end_time,
                "crossesMidnight": ss.end_time <= ss.start_time,
            }
        )
    return [{"region": r, "squads": squads} for r, squads in by_region.items()]


def get_recon_timeline(
    db: Session,
    *,
    date: str,
    region: str | None = None,
    scope_region: str | None = None,
    furnace: str | None = None,
    squad: str | None = None,
    status: str | None = None,
    q: str | None = None,
) -> dict:
    emp_ids: set[str] = (
        set(db.scalars(select(LaoqinPunch.emp_id).where(LaoqinPunch.natual_date == date)).all())
        | set(
            db.scalars(
                select(AttendanceRecord.emp_id).where(
                    AttendanceRecord.natual_date == date, scan_is_active()
                )
            ).all()
        )
        # 只有待审补录的人也要出现在对照里
        | set(
            db.scalars(
                select(AttendanceRecord.emp_id).where(
                    AttendanceRecord.natual_date == date, AttendanceRecord.status == "待审"
                )
            ).all()
        )
        # 只有加班记录的人（基础出勤可能在其他工作点）也要出现在对照里
        | set(
            db.scalars(
                select(AttendanceRecord.emp_id).where(
                    AttendanceRecord.natual_date == date,
                    AttendanceRecord.status == "active",
                    AttendanceRecord.record_kind == ATTENDANCE_KIND_OVERTIME,
                )
            ).all()
        )
    )

    # 性能：q 搜索前置——先把当天全体工号缩到「工号或姓名匹配 q」的少数人，
    # 再进入逐人多查询的重循环。否则同步进大量打卡后，会对当天全体(数千人)逐人查库，极慢。
    if q:
        needle = q.strip().lower()
        matched = {e for e in emp_ids if needle in e.lower()}
        # 姓名匹配：花名册很小，直接查名字含 q 的工号再与当天集合取交集（避免大 IN）
        name_hit = set(
            db.scalars(
                select(Employee.emp_id).where(func.lower(Employee.name).like(f"%{needle}%"))
            ).all()
        )
        emp_ids = matched | (name_hit & emp_ids)

    names = {
        e.emp_id: e.name
        for e in db.scalars(select(Employee).where(Employee.emp_id.in_(emp_ids))).all()
    }
    favorite_regions: dict[str, set[str]] = {}
    if emp_ids:
        for emp_id, value in db.execute(
            select(EmployeeFavorite.emp_id, EmployeeFavorite.value).where(
                EmployeeFavorite.emp_id.in_(emp_ids),
                EmployeeFavorite.kind == "region",
            )
        ):
            favorite_regions.setdefault(emp_id, set()).add(value)
    furnaces = {f.id: f for f in db.scalars(select(Furnace)).all()}

    items = []
    for emp_id in sorted(emp_ids):
        scans = db.scalars(
            select(AttendanceRecord)
            .where(
                AttendanceRecord.emp_id == emp_id,
                AttendanceRecord.natual_date == date,
                scan_is_active(),
            )
            .order_by(AttendanceRecord.scan_time)
        ).all()
        pending = db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.emp_id == emp_id,
                AttendanceRecord.natual_date == date,
                AttendanceRecord.status == "待审",
            )
        ).all()
        punches = get_laoqin_punches_for_recon(db, emp_id=emp_id, date=date)

        scan_items = []
        for sc in scans:
            f = furnaces.get(sc.furnace_id)
            # 并表后来源直接看 source：扫码 / 代签 / 补录（已审通过）
            if sc.source == "代签":
                origin = "代签"
            elif sc.source == "补录":
                origin = "补录通过"
            else:
                origin = "扫码"
            scan_items.append(
                {
                    "time": as_shanghai_datetime(sc.scan_time).isoformat() if sc.scan_time else None,
                    "region": f.region if f else None,
                    "furnace": f.furnace_no if f else None,
                    "squad": sc.squad,
                    "post": sc.post,
                    "employType": sc.employ_type,
                    "source": sc.source,
                    "origin": origin,  # 扫码 | 补录通过 | 代签
                    "workHours": sc.work_hours,  # 4=半班
                }
            )

        pending_makeups = []
        for m in pending:
            f = furnaces.get(m.furnace_id)
            pending_makeups.append(
                {
                    "region": f.region if f else None,
                    "furnace": f.furnace_no if f else None,
                    "squad": m.squad,
                    "post": m.post,
                    "employType": m.employ_type,
                    "reason": m.reason,
                    "workHours": m.work_hours,
                }
            )

        # 加班记录（record_kind=加班）：单列展示班次/岗位/时长
        overtime_items = []
        for ot in db.scalars(
            select(AttendanceRecord)
            .where(
                AttendanceRecord.emp_id == emp_id,
                AttendanceRecord.natual_date == date,
                AttendanceRecord.record_kind == ATTENDANCE_KIND_OVERTIME,
                AttendanceRecord.status == "active",
            )
            .order_by(AttendanceRecord.scan_time)
        ):
            f = furnaces.get(ot.furnace_id)
            overtime_items.append(
                {
                    "id": ot.id,
                    "time": as_shanghai_datetime(ot.scan_time).isoformat() if ot.scan_time else None,
                    "region": f.region if f else None,
                    "furnace": f.furnace_no if f else None,
                    "squad": ot.squad,
                    "shiftSlot": ot.shift_slot,
                    "post": ot.post,
                    "employType": ot.employ_type,
                    "workHours": ot.work_hours,
                    "reconDingtalk": ot.recon_dingtalk,
                    "checkWindow": get_overtime_recon_detail(db, ot),
                }
            )

        # 客户端地区筛选保持精确匹配；账号地区范围只剔除明确属于其它地区的行。
        # 只有劳勤打卡、或只有待审补录但尚未落工作点的异常行仍需给本地区 HR 判断归属。
        loc_items = scan_items + overtime_items
        scope_loc_items = loc_items + pending_makeups
        known_regions = {item["region"] for item in scope_loc_items if item.get("region")}
        assigned_regions = favorite_regions.get(emp_id, set())
        if scope_region:
            # 当天工作点证据与异常列表保持一致；没有工作点证据时才回退员工明确地区。
            # 完全无归属的劳勤孤行继续标记 regionPending，供各地区人工判断归属。
            if known_regions and scope_region not in known_regions:
                continue
            if not known_regions and assigned_regions and scope_region not in assigned_regions:
                continue
        if region and not any(s["region"] == region for s in loc_items):
            continue
        if furnace and not any(s["furnace"] == furnace for s in loc_items):
            continue
        if squad and not any(s["squad"] == squad for s in loc_items):
            continue
        name = names.get(emp_id) or emp_id
        if q:
            needle = q.strip().lower()
            if needle not in emp_id.lower() and needle not in (name or "").lower():
                continue

        recon = get_recon_for(db, emp_id, date)
        if status and recon["status"] != status:
            continue

        if scope_region:
            scan_items = [item for item in scan_items if item.get("region") == scope_region]
            overtime_items = [
                item for item in overtime_items if item.get("region") == scope_region
            ]
            pending_makeups = [
                item
                for item in pending_makeups
                if not item.get("region") or item.get("region") == scope_region
            ]

        items.append(
            {
                "empId": emp_id,
                "name": name,
                "status": recon["status"],
                "timeDiffMinutes": recon.get("timeDiffMinutes"),
                "scans": scan_items,
                "overtimes": overtime_items,
                "pendingMakeups": pending_makeups,
                "regionPending": bool(
                    scope_region and not known_regions and not assigned_regions
                ),
                "punches": [
                    {
                        "time": as_shanghai_datetime(lp.punch_time).isoformat(),
                        "inoutType": lp.inout_type,
                        "source": lp.record_source,
                        "makeupReason": lp.makeup_reason,
                    }
                    for lp in punches
                ],
            }
        )

    return {"date": date, "squadSchedules": get_squad_schedules(db), "items": items}
