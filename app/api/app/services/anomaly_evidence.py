from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AttendanceRecord, Furnace, ExternalAttendanceResultRaw
from app.domain.timezone import as_shanghai_datetime
from app.scan_active import ATTENDANCE_KIND_OVERTIME, scan_is_active
from app.services.anomaly_service import get_overtime_validity
from app.services.recon_service import get_laoqin_punches_for_recon, get_recon_for


def get_anomaly_evidence(db: Session, *, emp_id: str, date: str) -> dict:
    scans = db.scalars(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            scan_is_active(),
        )
        .order_by(AttendanceRecord.scan_time)
    ).all()
    # 并表后「补录/代签」的考勤主张即非 active 的工作流记录（待审/拒绝）；通过的已是 active，
    # 落在上面 scans 里，避免重复展示。
    makeups = db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.source.in_(["补录", "代签"]),
            AttendanceRecord.status.in_(["待审", "拒绝"]),
        )
    ).all()
    laoqin = get_laoqin_punches_for_recon(db, emp_id=emp_id, date=date)
    # 加班记录（record_kind=加班）：单列证据，劳勤工作台/日结证据链可见
    overtimes = db.scalars(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            AttendanceRecord.record_kind == ATTENDANCE_KIND_OVERTIME,
            AttendanceRecord.status == "active",
        )
        .order_by(AttendanceRecord.scan_time)
    ).all()
    recon = get_recon_for(db, emp_id, date)
    attendance_result = db.scalar(
        select(ExternalAttendanceResultRaw).where(
            ExternalAttendanceResultRaw.user_code == emp_id,
            ExternalAttendanceResultRaw.source_withdrawn_at.is_(None),
            ExternalAttendanceResultRaw.kq_date == date,
        )
    )

    furnace_by_id = {f.id: f for f in db.scalars(select(Furnace)).all()}
    overtime_validity = {
        overtime.id: get_overtime_validity(db, overtime)
        for overtime in overtimes
    }

    def _f(fid: int) -> tuple[str, str]:
        f = furnace_by_id.get(fid)
        return (f.region, f.furnace_no) if f else ("—", "—")

    furnace_no = "—"
    region = "—"
    if scans:
        region, furnace_no = _f(scans[0].furnace_id)
    elif makeups:
        region, furnace_no = _f(makeups[0].furnace_id)
    elif overtimes:
        region, furnace_no = _f(overtimes[0].furnace_id)

    return {
        "empId": emp_id,
        "date": date,
        "furnace": furnace_no,
        "region": region,
        "status": recon["status"],
        "attendanceResultWorkHours": attendance_result.cqgs if attendance_result else None,
        "laoqinPunches": [
            {
                "id": lp.id,
                "naturalDate": lp.natual_date,
                "time": as_shanghai_datetime(lp.punch_time).isoformat(),
                "inoutType": lp.inout_type,
                "source": lp.record_source,
                "makeupReason": lp.makeup_reason,
            }
            for lp in laoqin
        ],
        "scanRecords": [
            {
                "id": sc.id,
                "naturalDate": sc.natual_date,
                "time": as_shanghai_datetime(sc.scan_time).isoformat() if sc.scan_time else None,
                "region": _f(sc.furnace_id)[0],
                "furnace": _f(sc.furnace_id)[1],
                "post": sc.post,
                "squad": sc.squad,
                "employType": sc.employ_type,
                "source": sc.source,
                "workHours": sc.work_hours,
            }
            for sc in scans
        ],
        "makeups": [
            {
                "id": m.id,
                "naturalDate": m.natual_date,
                "region": _f(m.furnace_id)[0],
                "furnace": _f(m.furnace_id)[1],
                "squad": m.squad,
                "post": m.post,
                "employType": m.employ_type,
                "kind": m.source,
                "status": m.status,
                "reason": m.reason,
                "approver": m.reviewed_by,
            }
            for m in makeups
        ],
        "overtimes": [
            {
                "id": ot.id,
                "naturalDate": ot.natual_date,
                "region": _f(ot.furnace_id)[0],
                "furnace": _f(ot.furnace_id)[1],
                "post": ot.post,
                "squad": ot.squad,
                "shiftSlot": ot.shift_slot,
                "employType": ot.employ_type,
                "workHours": ot.work_hours,
                "reconDingtalk": ot.recon_dingtalk,
                "approver": ot.reviewed_by,
                "status": overtime_validity[ot.id][0],
                "valid": overtime_validity[ot.id][1],
                "validReason": overtime_validity[ot.id][2],
            }
            for ot in overtimes
        ],
    }
