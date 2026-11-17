"""Scoped, read-only reconciliation for reports; never attach computed rows to a Session."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.domain.recon import shift_interval
from app.models import (
    AttendanceRecord,
    Employee,
    Furnace,
    ExternalAttendanceResultRaw,
    LaoqinPunch,
    MonthlyExportLock,
    ReconResult,
    SquadSchedule,
)
from app.scan_active import ATTENDANCE_KIND_OVERTIME
from app.services.recon_service import (
    _status_with_overtime,
    compute_base_recon_from_loaded,
    compute_overtime_recon_from_loaded,
)
from app.services.report_jobs import bounded_rows, check_report_deadline


@dataclass(frozen=True)
class ReportRecon:
    emp_id: str
    date: str
    status: str
    hr_status: str = "未确认"
    chief_status: str = "未确认"
    id: int | None = None
    detail: str | None = None
    hr_by: str | None = None
    hr_at: datetime | None = None
    chief_by: str | None = None
    chief_at: datetime | None = None
    delegate_furnace_id: int | None = None


class ReportSnapshot:
    def __init__(self, db: Session, *, date_from: str, date_to: str, employee_ids: set[str]):
        self.employee_ids = employee_ids
        self.date_from, self.date_to = date_from, date_to
        self.records: list[AttendanceRecord] = []
        self.records_by_key: dict[tuple[str, str], list[AttendanceRecord]] = {}
        self.punches_by_key: dict[tuple[str, str], list[LaoqinPunch]] = {}
        self.results: dict[tuple[str, str], ExternalAttendanceResultRaw] = {}
        self.employees: dict[str, Employee] = {}
        self.furnaces: dict[int, Furnace] = {}
        self.recons: dict[tuple[str, str], ReportRecon] = {}
        self._base: dict[tuple[str, str], dict] = {}
        self._overtime: dict[int, str] = {}
        self._schedules: dict[tuple[str, str], tuple[int, int]] = {}
        if not employee_ids:
            return

        # Every source query is restricted before materialization. Cross-furnace records
        # for the selected employees remain necessary for monthly absence/leave ownership.
        self.records = bounded_rows(
            db,
            select(AttendanceRecord)
            .where(
                AttendanceRecord.emp_id.in_(employee_ids),
                AttendanceRecord.natual_date.between(date_from, date_to),
            )
            .order_by(AttendanceRecord.id),
        )
        for record in self.records:
            self.records_by_key.setdefault((record.emp_id, record.natual_date), []).append(record)
        self.employees = {
            e.emp_id: e
            for e in bounded_rows(db, select(Employee).where(Employee.emp_id.in_(employee_ids)))
        }
        self.furnaces = {
            f.id: f
            for f in bounded_rows(
                db, select(Furnace).where(Furnace.id.in_({r.furnace_id for r in self.records}))
            )
        }
        for schedule in bounded_rows(
            db,
            select(SquadSchedule)
            .where(
                SquadSchedule.deleted.is_(False),
                SquadSchedule.region.in_({f.region for f in self.furnaces.values()}),
            )
            .order_by(SquadSchedule.id),
        ):
            self._schedules.setdefault(
                (schedule.region, schedule.squad),
                shift_interval(schedule.start_time, schedule.end_time),
            )
        punch_from = (date.fromisoformat(date_from) - timedelta(days=1)).isoformat()
        punch_to = (date.fromisoformat(date_to) + timedelta(days=1)).isoformat()
        for punch in bounded_rows(
            db,
            select(LaoqinPunch)
            .options(
                load_only(
                    LaoqinPunch.id,
                    LaoqinPunch.emp_id,
                    LaoqinPunch.natual_date,
                    LaoqinPunch.punch_time,
                    LaoqinPunch.inout_type,
                    LaoqinPunch.org_name,
                    raiseload=True,
                )
            )
            .where(
                LaoqinPunch.emp_id.in_(employee_ids),
                LaoqinPunch.natual_date.between(punch_from, punch_to),
            )
            .order_by(LaoqinPunch.punch_time),
        ):
            self.punches_by_key.setdefault((punch.emp_id, punch.natual_date), []).append(punch)
        self.results = {
            (r.user_code, r.kq_date): r
            for r in bounded_rows(
                db,
                select(ExternalAttendanceResultRaw)
                .options(
                    load_only(
                        ExternalAttendanceResultRaw.id,
                        ExternalAttendanceResultRaw.user_code,
                        ExternalAttendanceResultRaw.kq_date,
                        ExternalAttendanceResultRaw.cqgs,
                        ExternalAttendanceResultRaw.kggs,
                        ExternalAttendanceResultRaw.leave_01,
                        ExternalAttendanceResultRaw.leave_02,
                        ExternalAttendanceResultRaw.leave_03,
                        ExternalAttendanceResultRaw.leave_05,
                        ExternalAttendanceResultRaw.leave_07,
                        ExternalAttendanceResultRaw.leave_09,
                        ExternalAttendanceResultRaw.leave_10,
                        ExternalAttendanceResultRaw.leave_12,
                        ExternalAttendanceResultRaw.leave_13,
                        ExternalAttendanceResultRaw.business_02,
                        raiseload=True,
                    )
                )
                .where(
                    ExternalAttendanceResultRaw.user_code.in_(employee_ids),
                    ExternalAttendanceResultRaw.source_withdrawn_at.is_(None),
                    ExternalAttendanceResultRaw.kq_date.between(date_from, date_to),
                )
                .order_by(ExternalAttendanceResultRaw.id),
            )
        }
        existing = {
            (r.emp_id, r.date): r
            for r in bounded_rows(
                db,
                select(ReconResult).where(
                    ReconResult.emp_id.in_(employee_ids),
                    ReconResult.date.between(date_from, date_to),
                ),
            )
        }
        locks = {
            (r.month, r.furnace_id)
            for r in bounded_rows(
                db,
                select(MonthlyExportLock).where(
                    MonthlyExportLock.month.between(date_from[:7], date_to[:7]),
                    MonthlyExportLock.furnace_id.in_(self.furnaces),
                    MonthlyExportLock.locked.is_(True),
                ),
            )
        }
        for key in sorted(set(self.records_by_key) | set(existing)):
            check_report_deadline()
            emp_id, day = key
            records = self.records_by_key.get(key, [])
            active = [r for r in records if r.status == "active"]
            pending = next((r for r in records if r.status == "待审"), None)
            protected = any((day[:7], r.furnace_id) in locks for r in active)
            protected = protected or (
                pending is not None and (day[:7], pending.furnace_id) in locks
            )
            old = existing.get(key)
            if protected:
                if old:
                    self.recons[key] = ReportRecon(
                        **{field: getattr(old, field) for field in ReportRecon.__dataclass_fields__}
                    )
                continue
            employee = self.employees.get(emp_id)
            if not employee or employee.deleted or not (active or pending):
                continue  # Ignore stale materialized rows without deleting business data.
            status = _status_with_overtime(
                self.base(emp_id, day)["status"],
                any(
                    self.overtime_status(r) == "加班需核对"
                    for r in active
                    if r.record_kind == ATTENDANCE_KIND_OVERTIME
                ),
            )
            values = (
                {field: getattr(old, field) for field in ReportRecon.__dataclass_fields__}
                if old
                else {"emp_id": emp_id, "date": day}
            )
            values["status"] = status
            if old is None or old.hr_by is None:
                values["hr_status"] = "待值班长确认" if status != "自动一致" else "未确认"
            if old is None or old.chief_by is None:
                values["chief_status"] = "未确认"
            self.recons[key] = ReportRecon(**values)

    @classmethod
    def for_month(cls, db: Session, month: str, employee_ids: set[str]):
        import calendar

        year, mon = map(int, month.split("-"))
        return cls(
            db,
            date_from=f"{month}-01",
            date_to=f"{month}-{calendar.monthrange(year, mon)[1]:02d}",
            employee_ids=employee_ids,
        )

    def punches(self, emp_id: str, day: str) -> list[LaoqinPunch]:
        cur = date.fromisoformat(day)
        return [
            p
            for offset in (-1, 0, 1)
            for p in self.punches_by_key.get(
                (emp_id, (cur + timedelta(days=offset)).isoformat()), []
            )
        ]

    def base(self, emp_id: str, day: str) -> dict:
        key = emp_id, day
        if key not in self._base:
            check_report_deadline()
            self._base[key] = compute_base_recon_from_loaded(
                emp_id=emp_id,
                date=day,
                records=self.records_by_key.get(key, []),
                punches=self.punches(emp_id, day),
                attendance_result=self.results.get(key),
            )
        return self._base[key]

    def validity(self, emp_id: str, day: str, *, pending_makeup: bool = False):
        from app.services.anomaly_service import derive_validity

        recon = self.recons.get((emp_id, day))
        return derive_validity(
            status=self.base(emp_id, day)["status"],
            hr_status=recon.hr_status if recon else "未确认",
            chief_status=recon.chief_status if recon else "未确认",
            pending_makeup=pending_makeup,
        )

    def overtime_status(self, record: AttendanceRecord) -> str:
        if record.id not in self._overtime:
            check_report_deadline()
            furnace = self.furnaces.get(record.furnace_id)
            shift = self._schedules.get((furnace.region, record.shift_slot)) if furnace else None
            self._overtime[record.id] = compute_overtime_recon_from_loaded(
                record,
                punches=self.punches(record.emp_id, record.natual_date),
                shift=shift,
            )
        return self._overtime[record.id]

    def overtime_validity(self, record: AttendanceRecord):
        from app.services.anomaly_service import MATCHABLE_SQUADS, derive_overtime_result

        recon = self.recons.get((record.emp_id, record.natual_date))
        base_records = [
            r
            for r in self.records_by_key.get((record.emp_id, record.natual_date), [])
            if r.record_kind != ATTENDANCE_KIND_OVERTIME and r.status in ("active", "待审")
        ]
        pending = not any(r.status == "active" for r in base_records) and any(
            r.status == "待审" and r.source == "补录" for r in base_records
        )
        base_valid, _ = self.validity(record.emp_id, record.natual_date, pending_makeup=pending)
        return derive_overtime_result(
            status=self.overtime_status(record),
            hr_status=recon.hr_status if recon else "未确认",
            chief_status=recon.chief_status if recon else "未确认",
            base_valid=base_valid,
            matching_base_squad=record.squad
            if record.squad in MATCHABLE_SQUADS
            and any(r.squad == record.squad for r in base_records)
            else None,
        )
