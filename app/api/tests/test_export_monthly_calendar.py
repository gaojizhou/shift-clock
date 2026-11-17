"""月度「考勤表式」日历导出回归。

覆盖：列布局、每日有效工时折算编制位、全月合计、加班口径
（单班>8h 延时 + record_kind=加班 班次），以及无效日不计。
"""
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from app.models import AttendanceRecord, Furnace, ExternalAttendanceResultRaw, LaoqinPunch
from app.services.export_service import (
    ExportInputError,
    build_monthly_calendar,
    build_monthly_calendar_xlsx,
)
from app.services.anomaly_service import set_confirm
from app.services.recon_service import run_recon_for_date

_SH = ZoneInfo("Asia/Shanghai")


def _seed(
    db, emp, date, post, *, kind="出勤", work_hours=8.0, cover=True,
    furnace_no="35号工作点", squad="1班", employ_type="正式",
):
    f = db.query(Furnace).filter_by(furnace_no=furnace_no).first()
    y, m, d = (int(x) for x in date.split("-"))
    db.add(
        AttendanceRecord(
            emp_id=emp, furnace_id=f.id, squad=squad, post=post,
            employ_type=employ_type, work_hours=work_hours, record_kind=kind,
            scan_time=datetime(y, m, d, 8, tzinfo=_SH),
            source="扫码" if kind == "出勤" else "加班", natual_date=date,
        )
    )
    if cover:
        for h, mi, direct in ((1, 50, "进"), (10, 10, "出")):
            db.add(LaoqinPunch(
                emp_id=emp, punch_time=datetime(y, m, d, h, mi, tzinfo=_SH),
                inout_type=direct, natual_date=date,
            ))
        if db.query(ExternalAttendanceResultRaw).filter_by(user_code=emp, kq_date=date).first() is None:
            db.add(ExternalAttendanceResultRaw(
                user_code=emp, user_name=emp, kq_date=date,
                status_name="正常", cqgs=f"{work_hours:.5f}",
            ))
    db.commit()


def _row(meta, emp_id, post):
    return next(r for r in meta["rows"] if r[4] == emp_id and r[5] == post)


def test_calendar_layout_and_daily_cells(db_session):
    _seed(db_session, "T0001", "2026-05-06", "出料工")  # 整班 8h → 第6天=1
    _seed(db_session, "T0001", "2026-05-07", "出料工", work_hours=4.0)  # 半班 → 第7天=0.5

    meta = build_monthly_calendar(db_session, month="2026-05")
    assert meta["daysInMonth"] == 31
    assert meta["columns"][:6] == ["地区", "工作点编号", "班组", "姓名", "工号", "岗位"]
    assert meta["columns"][6] == "1" and meta["columns"][36] == "31"
    assert meta["columns"][-5:] == [
        "应出勤天数", "全月出勤", "年休假统计天数", "旷工天数", "加班合计"
    ]

    row = _row(meta, "T0001", "出料工")
    assert row[0] == "示例厂区A" and row[1:3] == ["35号工作点", "1班"]
    # 列6=第1天 … 第6天在 index 6+5=11、第7天 index 12
    assert row[6 + 5] == 1       # 5/6 整班 → 1
    assert row[6 + 6] == 0.5     # 5/7 半班 → 0.5
    assert row[6 + 0] == ""      # 5/1 无记录 → 空
    assert row[-5] == 27         # 31 天 - 4 个公休
    assert row[-4] == 1.5        # 全月出勤 = 1 + 0.5（按折算天数展示）
    assert row[-3] == 0          # 无年休假
    # 原始考勤未提供旷工小时数，不再按出勤缺口推算
    assert row[-2] == 0
    assert row[-1] == ""         # 无加班

    blob = build_monthly_calendar_xlsx(meta)
    assert blob[:2] == b"PK"
    ws = load_workbook(BytesIO(blob)).active
    assert ws["E2"].value == "T0001"
    assert ws["E2"].data_type == "s"
    assert ws["E2"].number_format == "@"


@pytest.mark.parametrize(
    ("emp_id", "employ_type"),
    [("T0003", "实习"), ("T0005", "待岗")],
)
def test_calendar_exports_nonformal_half_shift_as_half_day(db_session, emp_id, employ_type):
    """实习/待岗半班沿用通用工时口径：4h 在月历和全月出勤中均折算为 0.5。"""
    _seed(
        db_session,
        emp_id,
        "2026-05-08",
        "出料工",
        work_hours=4.0,
        employ_type=employ_type,
    )

    row = _row(build_monthly_calendar(db_session, month="2026-05"), emp_id, "出料工")
    assert row[6 + 7] == 0.5
    assert row[-4] == 0.5


def test_calendar_overtime_delay_and_kind(db_session):
    # 单班 12h（整班8+延时4）→ 当天格=1（封顶），加班 +4
    _seed(db_session, "T0001", "2026-05-06", "出料工", work_hours=12.0)
    # 独立加班班次 4h（record_kind=加班）→ 不占日历格，加班 +4
    _seed(db_session, "T0001", "2026-05-06", "出料工", kind="加班", work_hours=4.0, cover=False)
    run_recon_for_date(db_session, "2026-05-06")
    set_confirm(db_session, emp_id="T0001", date="2026-05-06", kind="hr", status="通过", actor="HR01")

    meta = build_monthly_calendar(db_session, month="2026-05")
    row = _row(meta, "T0001", "出料工")
    assert row[6 + 5] == 1       # 5/6 出勤格封顶为 1（12h 不算 1.5）
    assert row[-4] == 1          # 全月编制位 = 1
    assert row[-1] == 8          # 加班 = 延时4 + 加班班次4


def test_calendar_invalid_day_excluded(db_session):
    # 无劳勤打卡 → 对账「工作点有劳勤无」→ 无效，不计入日历
    _seed(db_session, "T0003", "2026-05-06", "出料工", cover=False)
    meta = build_monthly_calendar(db_session, month="2026-05")
    assert not any(r[4] == "T0003" for r in meta["rows"])


def test_calendar_keeps_different_squads_as_separate_rows(db_session):
    _seed(db_session, "T0001", "2026-05-06", "出料工", squad="1班")
    _seed(db_session, "T0001", "2026-05-07", "出料工", squad="2班")
    result = db_session.query(ExternalAttendanceResultRaw).filter_by(
        user_code="T0001", kq_date="2026-05-06"
    ).one()
    result.kggs = "10"  # 有效满班的日期不重复计旷工
    db_session.add(ExternalAttendanceResultRaw(user_code="T0001", kq_date="2026-05-08", kggs="8"))
    db_session.add(ExternalAttendanceResultRaw(user_code="T0001", kq_date="2026-05-09", kggs="2"))
    db_session.commit()

    meta = build_monthly_calendar(db_session, month="2026-05")
    rows = [row for row in meta["rows"] if row[4] == "T0001"]
    assert {row[2] for row in rows} == {"1班", "2班"}
    assert sum(row[-2] for row in rows) == 1.25
    assert sum(row[-2] > 0 for row in rows) == 1


def test_calendar_bad_month(db_session):
    with pytest.raises(ExportInputError):
        build_monthly_calendar(db_session, month="2026-13")


def test_current_month_calendar_exposes_expected_days_and_warning(db_session, monkeypatch):
    from datetime import date
    from app.services import export_service

    monkeypatch.setattr(export_service, "_shanghai_today", lambda: date(2026, 8, 17))
    _seed(db_session, "T0001", "2026-08-06", "出料工")

    meta = build_monthly_calendar(db_session, month="2026-08")
    row = _row(meta, "T0001", "出料工")
    assert row[-5] == 14
    assert meta["warnings"][0]["type"] == "in_progress_month"

    workbook = load_workbook(BytesIO(build_monthly_calendar_xlsx(meta)))
    notes = [
        value
        for excel_row in workbook.active.iter_rows(values_only=True)
        for value in excel_row
        if isinstance(value, str)
    ]
    assert any("仅统计到 2026-08-16" in value for value in notes)
