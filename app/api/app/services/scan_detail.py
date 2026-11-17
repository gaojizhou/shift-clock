"""Build per-day attendance detail from scan records."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.timezone import as_shanghai_datetime
from app.models import (
    AttendanceRecord,
    ChiefConfirm,
    Employee,
    Furnace,
    FurnaceChief,
    FurnaceType,
)
from app.scan_active import ATTENDANCE_KIND_OVERTIME, scan_is_active


def _source_label(db: Session, *, emp_id: str, natual_date: str, row_source: str | None) -> str:
    """数据来源：扫码 / 补录 / 代签。
    扫码记录 source=扫码 即当天扫码；source=补录 再看补录单 kind 是否为代签。"""
    if (row_source or "扫码") == "扫码":
        return "扫码"
    mk = db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == natual_date,
            AttendanceRecord.source == "代签",
            scan_is_active(),
        )
    )
    return "代签" if mk else "补录"


def _chief_names(db: Session, furnace_id: int) -> str | None:
    """该工作点的值班长姓名（一工作点多值班长则顿号拼接）。"""
    rows = db.scalars(select(FurnaceChief).where(FurnaceChief.furnace_id == furnace_id)).all()
    names: list[str] = []
    for fc in rows:
        e = db.scalar(select(Employee).where(Employee.emp_id == fc.emp_id))
        names.append(e.name if e else fc.emp_id)
    return "、".join(names) if names else None


def _overtime_details(db: Session, *, emp_id: str, natual_date: str) -> list[dict]:
    """当天本人加班记录（record_kind=加班）：工人日历/首页也要看到自己的加班。"""
    out: list[dict] = []
    for ot in db.scalars(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == natual_date,
            AttendanceRecord.record_kind == ATTENDANCE_KIND_OVERTIME,
            AttendanceRecord.status == "active",
        )
        .order_by(AttendanceRecord.scan_time)
    ):
        fo = db.get(Furnace, ot.furnace_id)
        out.append(
            {
                "furnace": fo.furnace_no if fo else "",
                "region": fo.region if fo else "",
                "post": ot.post,
                "squad": ot.squad,
                "employType": ot.employ_type,
                "workHours": ot.work_hours if ot.work_hours is not None else 8.0,
                "reconDingtalk": ot.recon_dingtalk,
            }
        )
    return out


class ScanDetailContext:
    """Monthly calendar inputs, scoped to one employee and loaded in a fixed query count."""

    def __init__(self, db: Session, emp_id: str, date_from: str, date_to: str):
        work = list(
            db.scalars(
                select(AttendanceRecord)
                .where(
                    AttendanceRecord.emp_id == emp_id,
                    AttendanceRecord.natual_date.between(date_from, date_to),
                    scan_is_active(),
                )
                .order_by(AttendanceRecord.scan_time.desc())
            )
        )
        self.work_by_date = {}
        for row in work:
            self.work_by_date.setdefault(row.natual_date, []).append(row)
        overtime = list(
            db.scalars(
                select(AttendanceRecord)
                .where(
                    AttendanceRecord.emp_id == emp_id,
                    AttendanceRecord.natual_date.between(date_from, date_to),
                    AttendanceRecord.record_kind == ATTENDANCE_KIND_OVERTIME,
                    AttendanceRecord.status == "active",
                )
                .order_by(AttendanceRecord.scan_time)
            )
        )
        self.overtime_by_date = {}
        for row in overtime:
            self.overtime_by_date.setdefault(row.natual_date, []).append(row)
        fids = {r.furnace_id for r in [*work, *overtime]}
        self.furnaces = {f.id: f for f in db.scalars(select(Furnace).where(Furnace.id.in_(fids)))}
        self.types = {
            f.code: f.name
            for f in db.scalars(
                select(FurnaceType).where(
                    FurnaceType.code.in_({f.furnace_type_code for f in self.furnaces.values()}),
                )
            )
        }
        self.chiefs = {}
        for fc, name in db.execute(
            select(FurnaceChief, Employee.name)
            .outerjoin(
                Employee,
                Employee.emp_id == FurnaceChief.emp_id,
            )
            .where(FurnaceChief.furnace_id.in_(fids))
        ):
            self.chiefs.setdefault(fc.furnace_id, []).append(
                name if name is not None else fc.emp_id
            )
        self.confirmations = {}
        for cc in db.scalars(
            select(ChiefConfirm).where(
                ChiefConfirm.furnace_id.in_(fids),
                ChiefConfirm.natual_date.between(date_from, date_to),
            )
        ):
            self.confirmations.setdefault((cc.furnace_id, cc.natual_date), cc)

    def overtime_details(self, natual_date: str):
        out = []
        for ot in self.overtime_by_date.get(natual_date, []):
            fo = self.furnaces.get(ot.furnace_id)
            out.append(
                {
                    "furnace": fo.furnace_no if fo else "",
                    "region": fo.region if fo else "",
                    "post": ot.post,
                    "squad": ot.squad,
                    "employType": ot.employ_type,
                    "workHours": ot.work_hours if ot.work_hours is not None else 8.0,
                    "reconDingtalk": ot.recon_dingtalk,
                }
            )
        return out


def day_scan_detail(
    db: Session, *, emp_id: str, natual_date: str, context: ScanDetailContext | None = None
) -> dict | None:
    row = (
        next(iter(context.work_by_date.get(natual_date, [])), None)
        if context
        else db.scalar(
            select(AttendanceRecord)
            .where(
                AttendanceRecord.emp_id == emp_id,
                AttendanceRecord.natual_date == natual_date,
                scan_is_active(),
            )
            .order_by(AttendanceRecord.scan_time.desc())
            .limit(1)
        )
    )
    if not row:
        return None
    f = context.furnaces.get(row.furnace_id) if context else db.get(Furnace, row.furnace_id)
    furnace_type = None
    chief = None
    confirm = None
    if f:
        if f.furnace_type_code:
            if context:
                furnace_type = context.types.get(f.furnace_type_code)
            else:
                ft = db.get(FurnaceType, f.furnace_type_code)
                furnace_type = ft.name if ft else None
        chief = (
            ("、".join(context.chiefs.get(f.id, [])) or None) if context else _chief_names(db, f.id)
        )
        cc = (
            context.confirmations.get((f.id, natual_date))
            if context
            else db.scalar(
                select(ChiefConfirm).where(
                    ChiefConfirm.furnace_id == f.id, ChiefConfirm.natual_date == natual_date
                )
            )
        )
        if cc and cc.locked:
            confirm = "已确认"
        elif cc:
            confirm = "待确认"
        else:
            confirm = "未提交"
    if context:
        source = (
            "扫码"
            if (row.source or "扫码") == "扫码"
            else (
                "代签"
                if any(r.source == "代签" for r in context.work_by_date.get(natual_date, []))
                else "补录"
            )
        )
    else:
        source = _source_label(db, emp_id=emp_id, natual_date=natual_date, row_source=row.source)
    return {
        "furnace": f.furnace_no if f else "",
        "region": f.region if f else "",
        "furnaceType": furnace_type,
        "chief": chief,
        "post": row.post,
        "squad": row.squad,
        "employType": row.employ_type,
        # 出勤工时透出：半班=4.0/整班=8.0，与配额/算薪口径一致（旧数据空按 8.0）
        "workHours": row.work_hours if row.work_hours is not None else 8.0,
        "scanTime": as_shanghai_datetime(row.scan_time).isoformat() if row.scan_time else None,
        "source": source,
        "confirmStatus": confirm,
        "overtimes": (
            context.overtime_details(natual_date)
            if context
            else _overtime_details(db, emp_id=emp_id, natual_date=natual_date)
        ),
    }
