from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.errors import ConflictError
from app.models import (
    AttendanceRecord,
    Employee,
    EmployeeFavorite,
    Furnace,
    MonthlyExportLock,
    ReconResult,
    Region,
)
from app.services.confirm_service import set_work_hours
from app.services.monthly_export_lock_service import (
    assert_month_range_unlocked,
    set_month_locks,
)
from app.services.recon_service import run_recon_for_date


HR = {"x-test-user": "HR01", "x-test-role": "hr"}
STAT = {"x-test-user": "STAT01", "x-test-role": "stat"}
CHIEF = {"x-test-user": "L0001", "x-test-role": "chief"}
WORKER = {"x-test-user": "T0001", "x-test-role": "worker"}


def _furnace_id(db_session, furnace_no: str) -> int:
    return db_session.scalar(select(Furnace.id).where(Furnace.furnace_no == furnace_no))


def _lock(client, furnace_ids, month="2026-04", locked=True, headers=HR):
    return client.put(
        "/api/v1/export/month-lock",
        json={"month": month, "furnaceIds": furnace_ids, "locked": locked},
        headers=headers,
    )


def _scan(client, *, emp_id="T0001", furnace="35号工作点", day="2026-04-12"):
    return client.post(
        "/api/v1/scan",
        json={
            "userid": emp_id,
            "region": "示例厂区A",
            "furnace": furnace,
            "squad": "3班",
            "post": "出料工",
            "employType": "正式",
            "natualDate": day,
        },
        headers={"x-test-user": emp_id, "x-test-role": "worker"},
    )


def test_stat_can_read_furnace_lock_summary_but_cannot_change(client, db_session):
    furnace_id = _furnace_id(db_session, "35号工作点")
    assert _lock(client, [furnace_id]).status_code == 200

    r = client.get("/api/v1/export/month-lock", params={"month": "2026-04"}, headers=STAT)
    assert r.status_code == 200
    assert (r.json()["lockedCount"], r.json()["totalCount"]) == (1, 2)
    assert next(x for x in r.json()["items"] if x["furnaceId"] == furnace_id)["locked"] is True

    r = client.get("/api/v1/export/month-locks", params={"year": "2026"}, headers=STAT)
    assert r.status_code == 200
    april = next(x for x in r.json()["items"] if x["month"] == "2026-04")
    may = next(x for x in r.json()["items"] if x["month"] == "2026-05")
    assert april == {"month": "2026-04", "lockedCount": 1, "totalCount": 2}
    assert may == {"month": "2026-05", "lockedCount": 0, "totalCount": 2}

    r = _lock(client, [furnace_id], locked=False, headers=STAT)
    assert r.status_code == 403


def test_chief_can_read_only_own_furnace_lock_and_cannot_change(client, db_session):
    furnace_id = _furnace_id(db_session, "35号工作点")
    assert _lock(client, [furnace_id]).status_code == 200

    r = client.get(
        "/api/v1/export/month-lock",
        params={"month": "2026-04", "furnaceId": furnace_id},
        headers=CHIEF,
    )
    assert r.status_code == 200
    assert r.json()["items"] == [
        {
            "month": "2026-04",
            "furnaceId": furnace_id,
            "locked": True,
            "lockedBy": "HR01",
            "lockedAt": r.json()["items"][0]["lockedAt"],
            "unlockedBy": None,
            "unlockedAt": None,
        }
    ]

    r = _lock(client, [furnace_id], locked=False, headers=CHIEF)
    assert r.status_code == 403


def test_lock_is_per_furnace_and_unlock_restores_only_that_furnace(client, db_session):
    locked_id = _furnace_id(db_session, "35号工作点")
    unlocked_id = _furnace_id(db_session, "11号工作点")
    assert _lock(client, [locked_id]).status_code == 200

    blocked = _scan(client, emp_id="T0001", furnace="35号工作点")
    assert blocked.status_code == 409
    assert "35号工作点月度导出已锁定" in blocked.json()["detail"]

    assert _scan(client, emp_id="T0003", furnace="11号工作点").status_code == 201

    assert _lock(client, [locked_id], locked=False).status_code == 200
    assert _scan(client, emp_id="T0001", furnace="35号工作点").status_code == 201
    rows = db_session.scalars(select(MonthlyExportLock).where(MonthlyExportLock.month == "2026-04")).all()
    assert {(row.furnace_id, row.locked) for row in rows} == {(locked_id, False)}
    assert unlocked_id not in {row.furnace_id for row in rows}


def test_batch_lock_and_unlock_returns_month_summary(client, db_session):
    ids = [
        _furnace_id(db_session, "35号工作点"),
        _furnace_id(db_session, "11号工作点"),
    ]
    locked = _lock(client, ids).json()
    assert (locked["lockedCount"], locked["totalCount"]) == (2, 2)
    assert all(item["locked"] for item in locked["items"])

    unlocked = _lock(client, ids, locked=False).json()
    assert (unlocked["lockedCount"], unlocked["totalCount"]) == (0, 2)
    assert not any(item["locked"] for item in unlocked["items"])


def test_hr_region_scope_applies_to_lock_read_and_write(client, db_session):
    furnaces = db_session.scalars(select(Furnace).order_by(Furnace.id)).all()
    furnaces[0].region = "甲地区"
    furnaces[1].region = "乙地区"
    db_session.add_all(
        [
            Region(code="LOCK_A", name="甲地区", sort=1),
            Region(code="LOCK_B", name="乙地区", sort=2),
            Employee(emp_id="HR_SCOPE_LOCK", name="范围人事", role="hr"),
            EmployeeFavorite(emp_id="HR_SCOPE_LOCK", kind="region", value="甲地区"),
        ]
    )
    db_session.commit()
    scoped_hr = {"x-test-user": "HR_SCOPE_LOCK", "x-test-role": "hr"}

    denied = _lock(client, [furnaces[1].id], headers=scoped_hr)
    assert denied.status_code == 403

    allowed = _lock(client, [furnaces[0].id], headers=scoped_hr)
    assert allowed.status_code == 200
    assert (allowed.json()["lockedCount"], allowed.json()["totalCount"]) == (1, 1)
    assert [item["furnaceId"] for item in allowed.json()["items"]] == [furnaces[0].id]


def test_locked_furnace_blocks_confirm_hours_and_daily_submit(client, db_session):
    furnace = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    db_session.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=furnace.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            scan_time=datetime.now(timezone.utc),
            source="扫码",
            natual_date="2026-04-12",
            work_hours=8.0,
        )
    )
    db_session.commit()
    set_month_locks(
        db_session,
        month="2026-04",
        furnace_ids=[furnace.id],
        locked=True,
        actor="HR01",
    )

    with pytest.raises(ConflictError, match="35号工作点月度导出已锁定"):
        set_work_hours(
            db_session,
            furnace_no="35号工作点",
            region="示例厂区A",
            date="2026-04-12",
            emp_id="T0001",
            work_hours=4.0,
            actor="HR01",
        )

    submit = client.post(
        "/api/v1/confirm/submit",
        json={
            "furnace": "35号工作点",
            "region": "示例厂区A",
            "date": "2026-04-12",
            "locked": True,
        },
        headers=CHIEF,
    )
    assert submit.status_code == 409
    assert "35号工作点月度导出已锁定" in submit.json()["detail"]


def test_cross_furnace_range_task_is_blocked_if_any_furnace_is_locked(db_session):
    furnace_id = _furnace_id(db_session, "35号工作点")
    set_month_locks(
        db_session,
        month="2026-04",
        furnace_ids=[furnace_id],
        locked=True,
        actor="HR01",
    )

    with pytest.raises(ConflictError, match="2026-04（1个工作点）"):
        assert_month_range_unlocked(db_session, "2026-04-01", "2026-04-30")


def test_new_furnace_inherits_locked_months_of_its_region(client, db_session):
    """封账后新建的工作点必须继承本地区的已封账月份，否则锁记录缺失会让
    已关账月份的写路径被放行（月锁语义退化为『对封账时刻已有工作点打标』）。"""
    region = Region(code="NEWFUR", name="新工作点地区", sort=140)
    base_furnace = Furnace(region="新工作点地区", furnace_no="新工作点1号")
    db_session.add_all([region, base_furnace])
    db_session.commit()
    set_month_locks(
        db_session, month="2026-04", furnace_ids=[base_furnace.id], locked=True, actor="HR01"
    )

    created = client.post(
        "/api/v1/admin/dict/furnace",
        json={"name": "新工作点2号", "regionCode": "NEWFUR", "furnaceType": "标准型A"},
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert created.status_code == 200, created.text

    from app.services.monthly_export_lock_service import assert_month_range_unlocked

    new_furnace_id = db_session.scalar(
        select(Furnace.id).where(Furnace.furnace_no == "新工作点2号", Furnace.region == "新工作点地区")
    )
    inherited = db_session.scalar(
        select(MonthlyExportLock).where(
            MonthlyExportLock.furnace_id == new_furnace_id,
            MonthlyExportLock.month == "2026-04",
        )
    )
    assert inherited is not None and inherited.locked is True
    with pytest.raises(ConflictError):
        assert_month_range_unlocked(db_session, "2026-04-01", "2026-04-30", [new_furnace_id])


def test_furnace_region_migrate_inherits_target_locked_months(client, db_session):
    source = Region(code="MIGSRC", name="迁出地区", sort=142)
    target = Region(code="MIGDST", name="迁入地区", sort=143)
    moving = Furnace(region="迁出地区", furnace_no="迁移工作点")
    target_existing = Furnace(region="迁入地区", furnace_no="迁入已有")
    db_session.add_all([source, target, moving, target_existing])
    db_session.commit()
    set_month_locks(
        db_session, month="2026-04", furnace_ids=[target_existing.id], locked=True, actor="HR01"
    )

    updated = client.put(
        f"/api/v1/furnaces/{moving.id}/config",
        json={"region": "迁入地区"},
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert updated.status_code == 200, updated.text
    inherited = db_session.scalar(
        select(MonthlyExportLock).where(
            MonthlyExportLock.furnace_id == moving.id,
            MonthlyExportLock.month == "2026-04",
        )
    )
    assert inherited is not None and inherited.locked is True


def test_furnace_created_without_locked_month_stays_unlocked(client, db_session):
    region = Region(code="NOLOCK", name="无锁地区", sort=141)
    db_session.add(region)
    db_session.commit()

    created = client.post(
        "/api/v1/admin/dict/furnace",
        json={"name": "无锁1号", "regionCode": "NOLOCK", "furnaceType": "标准型A"},
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert created.status_code == 200, created.text

    furnace_id = db_session.scalar(
        select(Furnace.id).where(Furnace.furnace_no == "无锁1号")
    )
    assert (
        db_session.scalar(
            select(MonthlyExportLock).where(MonthlyExportLock.furnace_id == furnace_id)
        )
        is None
    )


def test_confirm_without_furnace_is_rejected_during_month_lock(client, db_session):
    """员工日无法定位工作点（如有劳勤打卡无扫码）时，封账期间必须保守拒绝确认，
    不能跳过月锁校验放行；未封账时照常允许。"""
    date = "2026-04-20"
    furnace_id = _furnace_id(db_session, "35号工作点")
    db_session.add(ReconResult(emp_id="T0005", date=date, status="工作点有劳勤无"))
    db_session.commit()
    payload = {"empId": "T0005", "date": date, "kind": "hr", "status": "通过"}
    headers = {"x-test-user": "HR01", "x-test-role": "hr"}

    unlocked = client.post("/api/v1/anomalies/confirm", json=payload, headers=headers)
    assert unlocked.status_code == 200, unlocked.text

    db_session.add(ReconResult(emp_id="T0003", date=date, status="工作点有劳勤无"))
    db_session.commit()
    assert _lock(client, [furnace_id]).status_code == 200

    locked = client.post(
        "/api/v1/anomalies/confirm",
        json={"empId": "T0003", "date": date, "kind": "hr", "status": "通过"},
        headers=headers,
    )
    assert locked.status_code == 409
    assert "归属不明确" in locked.json()["detail"]
    row = db_session.query(ReconResult).filter_by(emp_id="T0003", date=date).one()
    assert row.hr_status == "未确认"


def test_confirm_blocked_when_any_same_day_furnace_is_locked(client, db_session):
    """确认写的是员工日对账，任一当日工作点已封账都必须 409，不能只校验本地区回退工作点。"""
    date = "2026-06-15"
    local = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    remote = Furnace(region="外地地区", furnace_no="外地1号工作点", furnace_type_code="FT01")
    db_session.add_all([
        Region(code="REMOTE", name="外地地区", sort=160),
        Region(code="SHZ", name="示例厂区A", sort=1),
        remote,
        Employee(emp_id="CROSS_HR", name="示例厂区A人事", role="hr"),
        EmployeeFavorite(emp_id="CROSS_HR", kind="region", value="示例厂区A"),
        EmployeeFavorite(emp_id="T0001", kind="region", value="示例厂区A"),
    ])
    db_session.flush()
    now = datetime.now(timezone.utc)
    for furnace in (local, remote):
        db_session.add(
            AttendanceRecord(
                emp_id="T0001",
                furnace_id=furnace.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                scan_time=now,
                source="扫码",
                natual_date=date,
                status="active",
                work_hours=4.0,
            )
        )
    db_session.add(ReconResult(emp_id="T0001", date=date, status="时间错位"))
    db_session.commit()
    assert _lock(client, [remote.id], month="2026-06").status_code == 200

    blocked = client.post(
        "/api/v1/anomalies/confirm",
        json={"empId": "T0001", "date": date, "kind": "hr", "status": "不通过"},
        headers={"x-test-user": "CROSS_HR", "x-test-role": "hr"},
    )
    assert blocked.status_code == 409, blocked.text
    row = db_session.query(ReconResult).filter_by(emp_id="T0001", date=date).one()
    assert row.hr_status == "未确认"


def test_recon_recalculation_skips_employees_on_locked_furnace(db_session):
    locked_furnace = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    unlocked_furnace = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "11号工作点"))
    for emp_id, furnace in (("T0001", locked_furnace), ("T0003", unlocked_furnace)):
        db_session.add(
            AttendanceRecord(
                emp_id=emp_id,
                furnace_id=furnace.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                scan_time=datetime.now(timezone.utc),
                source="扫码",
                natual_date="2026-04-12",
                work_hours=8.0,
            )
        )
    db_session.add(ReconResult(emp_id="T0001", date="2026-04-12", status="冻结状态"))
    db_session.commit()
    set_month_locks(
        db_session,
        month="2026-04",
        furnace_ids=[locked_furnace.id],
        locked=True,
        actor="HR01",
    )

    run_recon_for_date(db_session, "2026-04-12")

    locked_recon = db_session.scalar(
        select(ReconResult).where(ReconResult.emp_id == "T0001", ReconResult.date == "2026-04-12")
    )
    unlocked_recon = db_session.scalar(
        select(ReconResult).where(ReconResult.emp_id == "T0003", ReconResult.date == "2026-04-12")
    )
    assert locked_recon.status == "冻结状态"
    assert unlocked_recon is not None
