from datetime import datetime, timezone
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.models import (AttendanceRecord, AuditLog, Employee, EmployeeFavorite, Furnace,
                        ExternalAttendanceResultRaw, MonthlyAbsenceOverride, MonthlyExportLock, Region)
from app.services.export_service import build_export_meta, build_export_xlsx, build_monthly_calendar


@pytest.fixture
def absence_scope(db_session, client):
    furnace = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    db_session.add(AttendanceRecord(emp_id="T0001", furnace_id=furnace.id, squad="3班",
        post="出料工", employ_type="正式", scan_time=datetime.now(timezone.utc),
        source="扫码", natual_date="2026-04-01", work_hours=8, status="active", record_kind="出勤"))
    db_session.add(ExternalAttendanceResultRaw(user_code="T0001", kq_date="2026-04-28", kggs="8"))
    db_session.commit()
    client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})
    return furnace.id


def save(client, days, revision=0, emp_id="T0001", month="2026-04"):
    return client.put("/api/v1/export/absence-overrides", json={
        "month": month, "empId": emp_id, "days": days, "revision": revision,
    })


def test_override_shared_zero_clear_and_export_consistency(client, db_session, absence_scope):
    for value, revision, expected in [(2.25, 0, 2.25), (0, 1, 0), (None, 2, 1)]:
        response = save(client, value, revision)
        assert response.status_code == 200, response.text
        # 换一个用户仍能读取同一共享值。
        client.post("/api/v1/auth/login", json={"userid": "other-admin", "role": "admin"})
        shared = client.get(f"/api/v1/export/absence-overrides?month=2026-04&furnaceIds={absence_scope}")
        assert shared.json()["items"]["T0001"]["days"] == value
        meta = build_export_meta(db_session, month="2026-04", furnaces=[], furnace_ids=[absence_scope],
            emp_id="T0001", persist=False, payroll2_include_zero_rows=True)
        for key in ("payrollSheet", "payrollSheet2"):
            assert sum(row["旷工天数"] for row in meta[key]["rows"]) == expected
        calendar = build_monthly_calendar(db_session, month="2026-04", restrict_furnace_ids={absence_scope}, persist=False)
        index = calendar["columns"].index("旷工天数")
        assert sum(row[index] for row in calendar["rows"]) == expected
        workbook = load_workbook(BytesIO(build_export_xlsx(meta)))
        for name in ("输出给薪资", "输出给薪资2", "班次有效工时"):
            sheet = workbook[name]
            headers = [cell.value for cell in sheet[1]]
            col = headers.index("旷工天数") + 1
            assert sum(sheet.cell(i, col).value or 0 for i in range(2, sheet.max_row + 1)) == expected
    assert len(list(db_session.scalars(select(AuditLog).where(AuditLog.action == "monthly_absence_override")))) == 3


def test_conflict_does_not_overwrite_shared_value(client, db_session, absence_scope):
    assert save(client, 1.5).status_code == 200
    assert save(client, 9).status_code == 409
    assert save(client, None, 1).status_code == 200
    assert save(client, 9, 1).status_code == 409
    assert db_session.get(MonthlyAbsenceOverride, ("2026-04", "T0001")).days is None


@pytest.mark.parametrize("value", [-1, 1.234, 31, "NaN", "Infinity"])
def test_invalid_days_rejected(client, absence_scope, value):
    assert save(client, value).status_code in (400, 422)


def test_locked_employee_rejected_even_if_lock_outside_selected_furnace(client, db_session, absence_scope):
    other = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "11号工作点"))
    db_session.add(AttendanceRecord(emp_id="T0001", furnace_id=other.id, squad="3班", post="出料工",
        employ_type="正式", source="扫码", natual_date="2026-04-02", status="active", record_kind="出勤"))
    db_session.add(MonthlyExportLock(month="2026-04", furnace_id=other.id, locked=True))
    db_session.commit()
    assert save(client, 1).status_code == 409
    entry = client.get(f"/api/v1/export/absence-overrides?month=2026-04&furnaceIds={absence_scope}").json()["items"]["T0001"]
    assert entry["editable"] is False


def test_roles_regions_and_missing_employee(client, db_session, absence_scope):
    assert save(client, 1, emp_id="MISSING").status_code == 400
    client.post("/api/v1/auth/login", json={"userid": "T0001", "role": "worker"})
    assert save(client, 1).status_code == 403
    assert client.get(f"/api/v1/export/absence-overrides?month=2026-04&furnaceIds={absence_scope}").status_code == 403
    db_session.add_all([Region(code="OTHER", name="外区"),
        Employee(emp_id="HR_OTHER", dd_user_id="HR_OTHER", name="外区人事", role="hr"),
        EmployeeFavorite(emp_id="HR_OTHER", kind="region", value="外区")])
    db_session.commit()
    client.post("/api/v1/auth/login", json={"userid": "HR_OTHER", "role": "hr"})
    assert save(client, 1).status_code == 403
    assert client.get(f"/api/v1/export/absence-overrides?month=2026-04&furnaceIds={absence_scope}").status_code == 403


def test_override_not_duplicated_across_furnaces_or_posts(client, db_session, absence_scope):
    for post in ("出料工", "物料转运工"):
        db_session.add(AttendanceRecord(emp_id="T0001", furnace_id=absence_scope, squad="2班", post=post,
            employ_type="正式", source="扫码", natual_date="2026-04-02", status="active", record_kind="出勤"))
    db_session.commit()
    assert save(client, 2).status_code == 200
    meta = build_export_meta(db_session, month="2026-04", furnaces=[], furnace_ids=[absence_scope],
        emp_id="T0001", persist=False, payroll2_group_by=["工号", "班组", "岗位"], payroll2_include_zero_rows=True)
    assert sum(row["旷工天数"] for row in meta["payrollSheet2"]["rows"]) == 2
    index = meta["monthlyHours"]["columns"].index("旷工天数")
    assert sum(row[index] for row in meta["monthlyHours"]["rows"]) == 2


def test_manual_value_has_single_owner_when_exporting_furnaces_separately(client, db_session, absence_scope):
    from app.services.recon_service import run_recon_for_date
    from app.services.anomaly_service import set_confirm
    other = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "11号工作点"))
    for day, furnace_id in [("2026-04-02", absence_scope), ("2026-04-03", absence_scope), ("2026-04-04", other.id)]:
        db_session.add(AttendanceRecord(emp_id="T0001", furnace_id=furnace_id, squad="3班", post="出料工",
            employ_type="正式", work_hours=8, source="扫码", natual_date=day, status="active", record_kind="出勤"))
    db_session.commit()
    for day in ["2026-04-01", "2026-04-02", "2026-04-03", "2026-04-04"]:
        run_recon_for_date(db_session, day)
        set_confirm(db_session, emp_id="T0001", date=day, kind="hr", status="通过", actor="admin")
    assert save(client, 2.5).status_code == 200
    for furnace_id, expected in [(absence_scope, 2.5), (other.id, 0)]:
        meta = build_export_meta(db_session, month="2026-04", furnaces=[], furnace_ids=[furnace_id],
            emp_id="T0001", persist=False, payroll2_include_zero_rows=True)
        for key in ("payrollSheet", "payrollSheet2"):
            assert sum(row["旷工天数"] for row in meta[key]["rows"]) == expected
        hours = build_monthly_calendar(db_session, month="2026-04", restrict_furnace_ids={furnace_id}, persist=False)
        index = hours["columns"].index("旷工天数")
        assert sum(row[index] for row in hours["rows"]) == expected


@pytest.mark.parametrize("manual,expected_export", [(2.25, 2.25), (0, 0), (None, 1)])
def test_preview_keeps_automatic_absence_while_download_uses_manual(
    client, absence_scope, manual, expected_export,
):
    # 先保存再修改/清空，验证已有手动记录不会污染预览。
    assert save(client, 3).status_code == 200
    assert save(client, manual, revision=1).status_code == 200
    body = {"month": "2026-04", "furnaces": [], "furnaceIds": [absence_scope],
            "empId": "T0001", "payroll2IncludeZeroRows": True}

    def check_grid(grid, expected):
        index = grid["columns"].index("旷工天数")
        assert sum(float(row[index] or 0) for row in grid["rows"]) == expected

    def check_workbook(response, names):
        assert response.status_code == 200, response.text
        workbook = load_workbook(BytesIO(response.content))
        for name in names:
            sheet = workbook[name]
            index = [cell.value for cell in sheet[1]].index("旷工天数") + 1
            assert sum(float(sheet.cell(i, index).value or 0)
                       for i in range(2, sheet.max_row + 1)) == expected_export

    preview = client.post("/api/v1/export", json={**body, "format": "meta"})
    assert preview.status_code == 200, preview.text
    for key in ("payrollSheet", "payrollSheet2"):
        assert sum(float(row["旷工天数"] or 0) for row in preview.json()[key]["rows"]) == 1
    check_grid(preview.json()["monthlyHours"], 1)
    for sheet in ("输出给薪资", "输出给薪资2"):
        response = client.post("/api/v1/export/sheet-preview", json={**body, "sheet": sheet})
        assert response.status_code == 200, response.text
        assert sum(float(row["旷工天数"] or 0)
                   for row in response.json()["payrollSheet"]["rows"]) == 1
        check_workbook(client.post("/api/v1/export/sheet", json={**body, "sheet": sheet}), [sheet])
    hours_url = f"/api/v1/export/monthly-hours?month=2026-04&furnaceIds={absence_scope}"
    response = client.get(hours_url + "&format=meta")
    assert response.status_code == 200, response.text
    check_grid(response.json(), 1)
    check_workbook(client.get(hours_url), ["班次有效工时2026-04"])
    check_workbook(client.post("/api/v1/export", json=body),
                   ["输出给薪资", "输出给薪资2", "班次有效工时"])
    check_workbook(client.post("/api/v1/export/sheet", json={**body, "sheet": "班次有效工时"}),
                   ["班次有效工时"])


def _confirm_attendance(db_session, days):
    from app.services.recon_service import run_recon_for_date
    from app.services.anomaly_service import set_confirm
    for day in days:
        run_recon_for_date(db_session, day)
        set_confirm(db_session, emp_id="T0001", date=day, kind="hr", status="通过", actor="admin")


def test_manual_limit_uses_all_furnaces_daily_attendance_leave_and_yesterday(
    client, db_session, absence_scope, monkeypatch,
):
    from datetime import date
    from app.services import export_service
    monkeypatch.setattr(export_service, "_shanghai_today", lambda: date(2026, 4, 4))
    other = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "11号工作点"))
    # 同日跨工作点重复出勤只取最大值，不重复扣除；加班不占正常出勤额度。
    for furnace in [absence_scope, other.id]:
        db_session.add(AttendanceRecord(emp_id="T0001", furnace_id=furnace, squad="3班", post="出料工",
            employ_type="正式", source="扫码", natual_date="2026-04-02", work_hours=4,
            status="active", record_kind="出勤"))
    db_session.add_all([
        ExternalAttendanceResultRaw(user_code="T0001", kq_date="2026-04-02", leave_01="2"),
        ExternalAttendanceResultRaw(user_code="T0001", kq_date="2026-04-03", leave_01="8", leave_02="8"),
    ])
    db_session.commit()
    _confirm_attendance(db_session, ["2026-04-01", "2026-04-02"])
    for furnace in [absence_scope, other.id]:
        entry = client.get(f"/api/v1/export/absence-overrides?month=2026-04&furnaceIds={furnace}").json()["items"]["T0001"]
        assert entry["maxDays"] == 0.25  # 3天 - 1天出勤 - 半天出勤 - 0.25天请假 - 1天请假
    rejected = save(client, 0.26)
    assert rejected.status_code == 400
    assert "0.25" in rejected.json()["detail"]
    accepted = save(client, 0.25)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["maxDays"] == 0.25


@pytest.mark.parametrize("today", ["2026-04-01", "2026-03-31"])
def test_manual_limit_no_elapsed_days(client, absence_scope, monkeypatch, today):
    from datetime import date
    from app.services import export_service
    monkeypatch.setattr(export_service, "_shanghai_today", lambda: date.fromisoformat(today))
    assert save(client, 0.01).status_code == 400
    assert save(client, 0).json()["maxDays"] == 0
    assert save(client, None, revision=1).status_code == 200


def test_limit_rounds_down_and_respects_leap_month():
    from datetime import date
    from decimal import Decimal
    from types import SimpleNamespace
    from app.services.export_service import _monthly_absence_limits
    context = SimpleNamespace(results={("E", "2024-02-01"): SimpleNamespace(leave_01="0.01")})
    limit = _monthly_absence_limits(context, "2024-02", {}, ["E"], today=date(2024, 3, 1))["E"]
    assert limit == Decimal("28.99")


@pytest.mark.parametrize("automatic,manual", [(0, 2), (1, 0), (0, 0)])
def test_zero_attendance_keeps_manual_employee_in_default_preview_and_download(
    client, db_session, absence_scope, automatic, manual,
):
    # 明确将扫码日判为无效，避免有出勤行掩盖默认隐藏0值行的问题。
    from app.models import ReconResult
    db_session.add(ReconResult(emp_id="T0001", date="2026-04-01", status="异常", hr_status="不通过"))
    raw = db_session.scalar(select(ExternalAttendanceResultRaw).where(ExternalAttendanceResultRaw.user_code == "T0001"))
    raw.kggs = str(automatic * 8)
    db_session.commit()
    assert save(client, manual).status_code == 200
    body = {"month": "2026-04", "furnaces": [], "furnaceIds": [absence_scope]}
    preview = client.post("/api/v1/export", json={**body, "format": "meta"})
    assert preview.status_code == 200, preview.text
    for key in ["payrollSheet", "payrollSheet2"]:
        rows = preview.json()[key]["rows"]
        assert [r["工号"] for r in rows] == ["T0001"]
        assert rows[0]["实际出勤天数"] == 0
        assert rows[0]["旷工天数"] == automatic
    calendar = preview.json()["monthlyHours"]
    assert [r[4] for r in calendar["rows"]] == ["T0001"]
    assert calendar["rows"][0][calendar["columns"].index("旷工天数")] == automatic
    standalone = client.get(f"/api/v1/export/monthly-hours?month=2026-04&furnaceIds={absence_scope}&format=meta")
    assert standalone.status_code == 200
    assert [r[4] for r in standalone.json()["rows"]] == ["T0001"]
    download = client.post("/api/v1/export", json=body)
    assert download.status_code == 200
    workbook = load_workbook(BytesIO(download.content))
    for name in ["输出给薪资", "输出给薪资2", "班次有效工时"]:
        sheet = workbook[name]
        cols = [c.value for c in sheet[1]]
        rows = [r for r in sheet.iter_rows(min_row=2, values_only=True)
                if str(r[cols.index("工号")]).lstrip("'") == "T0001"]
        assert len(rows) == 1
        assert rows[0][cols.index("旷工天数")] == manual
    assert save(client, None, revision=1).status_code == 200
    if automatic == 0:
        cleared = client.post("/api/v1/export", json={**body, "format": "meta"}).json()
        assert cleared["payrollSheet"]["rows"] == []
        assert cleared["payrollSheet2"]["rows"] == []
        assert cleared["monthlyHours"]["rows"] == []


def test_export_rechecks_stale_manual_limit_and_still_allows_clear(client, db_session, absence_scope):
    assert save(client, 30).status_code == 200
    # 保存后补确认一天出勤，可填上限由30变为29。
    _confirm_attendance(db_session, ["2026-04-01"])
    entry = client.get(f"/api/v1/export/absence-overrides?month=2026-04&furnaceIds={absence_scope}").json()["items"]["T0001"]
    assert entry["days"] == 30 and entry["maxDays"] == 29 and entry["editable"]
    body = {"month": "2026-04", "furnaces": [], "furnaceIds": [absence_scope]}
    assert client.post("/api/v1/export", json={**body, "format": "meta"}).status_code == 200
    for response in [client.post("/api/v1/export", json=body),
                     client.post("/api/v1/export/sheet", json={**body, "sheet": "输出给薪资"}),
                     client.get(f"/api/v1/export/monthly-hours?month=2026-04&furnaceIds={absence_scope}")]:
        assert response.status_code == 400
        assert "请在手动输入列修改或清空" in response.json()["detail"]
    assert save(client, None, revision=1).status_code == 200
    assert client.post("/api/v1/export", json=body).status_code == 200
