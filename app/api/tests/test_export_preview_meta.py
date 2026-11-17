"""GET 导出接口 format=meta 预览回归：JSON 口径与下载一致、只读不落库、不写审计。"""
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from app.models import AttendanceRecord, AuditLog, Furnace, ExternalAttendanceResultRaw, LaoqinPunch, ReconResult

_SH = ZoneInfo("Asia/Shanghai")


def _seed(db, emp, date, post, *, work_hours=8.0, squad="2班"):
    """加一条 中班(10:00-18:00) 扫码出勤 + 覆盖打卡（对账自动一致→有效）。"""
    f = db.query(Furnace).filter_by(furnace_no="35号工作点").first()
    y, m, d = (int(x) for x in date.split("-"))
    db.add(AttendanceRecord(
        emp_id=emp, furnace_id=f.id, squad=squad, post=post, employ_type="正式",
        work_hours=work_hours, record_kind="出勤",
        scan_time=datetime(y, m, d, 12, tzinfo=_SH),
        source="扫码", natual_date=date,
    ))
    for h, mi, direct in ((9, 50, "进"), (18, 10, "出")):
        db.add(LaoqinPunch(emp_id=emp, punch_time=datetime(y, m, d, h, mi, tzinfo=_SH),
                           inout_type=direct, natual_date=date))
    db.add(ExternalAttendanceResultRaw(
        user_code=emp, user_name=emp, kq_date=date,
        status_name="正常", cqgs=f"{work_hours:.5f}",
    ))
    db.commit()


def _login_hr(client):
    client.post("/api/v1/auth/login", json={"userid": "HR01", "role": "hr"})


def test_monthly_hours_meta_preview_readonly(client, db_session):
    _seed(db_session, "T0001", "2026-05-06", "出料工")
    _login_hr(client)
    r = client.get("/api/v1/export/monthly-hours?month=2026-05&format=meta")
    assert r.status_code == 200
    meta = r.json()
    assert meta["month"] == "2026-05"
    assert meta["daysInMonth"] == 31
    assert meta["columns"][:6] == ["地区", "工作点编号", "班组", "姓名", "工号", "岗位"]
    assert meta["columns"][-4:] == ["全月出勤", "年休假统计天数", "旷工天数", "加班合计"]
    row = next(x for x in meta["rows"] if x[4] == "T0001")
    assert row[1:3] == ["35号工作点", "2班"] and row[5] == "出料工"
    # 6日格 = 8h/8 = 1 编制位（前6列固定 + 日1..31）
    assert row[6 + 6 - 1] == 1
    # 只读：预览不物化对账、不写审计
    assert db_session.query(ReconResult).count() == 0
    assert db_session.query(AuditLog).filter_by(action="export_monthly_hours").count() == 0


def test_overtime_detail_meta_preview_readonly(client, db_session):
    # 12h 出勤（有效）→ 延时 4h 加班
    _seed(db_session, "T0001", "2026-05-06", "出料工", work_hours=12.0)
    _login_hr(client)
    r = client.get("/api/v1/export/overtime-detail?month=2026-05&format=meta")
    assert r.status_code == 200
    meta = r.json()
    assert meta["columns"][0] == "序号"
    assert len(meta["rows"]) == 1
    assert meta["rows"][0][4] == 4.0  # 加班时间(小时)
    assert meta["summaryRows"] == [["35号工作点", "出料工", "2班", 4.0]]
    assert len(meta["byPersonRows"]) == 1
    assert db_session.query(ReconResult).count() == 0
    assert db_session.query(AuditLog).filter_by(action="export_overtime_detail").count() == 0


def test_export_sheet_preview_payroll_readonly(client, db_session):
    _seed(db_session, "T0001", "2026-05-06", "出料工")
    _login_hr(client)
    r = client.post(
        "/api/v1/export/sheet-preview",
        json={"month": "2026-05", "furnaces": ["35号工作点"], "sheet": "输出给薪资"},
    )
    assert r.status_code == 200
    meta = r.json()
    assert meta["sheet"] == "输出给薪资"
    assert meta["payrollSheet"]["rowCount"] >= 1
    assert "extraSheet" not in meta
    assert db_session.query(ReconResult).count() == 0
    assert db_session.query(AuditLog).filter_by(action="export_sheet").count() == 0


def test_export_single_sheet_download_has_one_sheet(client, db_session):
    _seed(db_session, "T0001", "2026-05-06", "出料工")
    _login_hr(client)
    r = client.post(
        "/api/v1/export/sheet",
        json={"month": "2026-05", "furnaces": ["35号工作点"], "sheet": "打卡异常"},
    )
    assert r.status_code == 200
    wb = load_workbook(BytesIO(r.content))
    assert wb.sheetnames == ["打卡异常"]
    assert db_session.query(AuditLog).filter_by(action="export_sheet").count() == 1


def test_monthly_hours_download_still_persists_audit(client, db_session):
    """下载保留文件和业务审计，但生成报表不再写对账表。"""
    _seed(db_session, "T0001", "2026-05-06", "出料工")
    _login_hr(client)
    r = client.get("/api/v1/export/monthly-hours?month=2026-05")
    assert r.status_code == 200
    assert r.content[:2] == b"PK"
    assert db_session.query(AuditLog).filter_by(action="export_monthly_hours").count() == 1
    assert db_session.query(ReconResult).count() == 0


def test_meta_preview_bad_month_400(client, db_session):
    _login_hr(client)
    assert client.get("/api/v1/export/monthly-hours?month=2026-13&format=meta").status_code == 400
    assert client.get("/api/v1/export/overtime-detail?month=2026-13&format=meta").status_code == 400
