"""加班明细导出回归：出勤延时>8h + 独立加班记录，仅有效日，对齐 Excel 9 列。"""
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import openpyxl
import pytest

from app.models import AttendanceRecord, Furnace, ExternalAttendanceResultRaw, LaoqinPunch
from app.services.export_service import (
    ExportInputError,
    build_overtime_detail,
    build_overtime_detail_xlsx,
)
from app.services.anomaly_service import set_confirm
from app.services.recon_service import run_recon_for_date

_SH = ZoneInfo("Asia/Shanghai")


def _seed(db, emp, date, post, *, kind="出勤", work_hours=8.0, reason=None, squad="2班", cover=True):
    f = db.query(Furnace).filter_by(furnace_no="35号工作点").first()
    y, m, d = (int(x) for x in date.split("-"))
    db.add(AttendanceRecord(
        emp_id=emp, furnace_id=f.id, squad=squad, post=post, employ_type="正式",
        work_hours=work_hours, record_kind=kind, reason=reason,
        scan_time=datetime(y, m, d, 12, tzinfo=_SH),
        source="扫码" if kind == "出勤" else "加班", natual_date=date,
    ))
    if cover:
        # 中班 10:00-18:00：打卡 09:50~18:10 覆盖 → 对账自动一致(有效)
        for h, mi, direct in ((9, 50, "进"), (18, 10, "出")):
            db.add(LaoqinPunch(emp_id=emp, punch_time=datetime(y, m, d, h, mi, tzinfo=_SH),
                               inout_type=direct, natual_date=date))
        if kind == "出勤" and db.query(ExternalAttendanceResultRaw).filter_by(user_code=emp, kq_date=date).first() is None:
            db.add(ExternalAttendanceResultRaw(
                user_code=emp, user_name=emp, kq_date=date,
                status_name="正常", cqgs=f"{work_hours:.5f}",
            ))
    db.commit()


def test_overtime_columns_and_sources(db_session):
    db = db_session
    # 出勤 12h（中班，打卡覆盖→有效）→ 延时 4h 加班
    _seed(db, "T0001", "2026-05-06", "出料工", work_hours=12.0, reason="缺出料工")
    # 独立加班记录 4h（晚班，与出勤不同班次避唯一索引冲突）；有加班需先确认通过才计入导出。
    _seed(db, "T0001", "2026-05-06", "供气工", kind="加班", work_hours=4.0, reason="岗位缺人", squad="3班", cover=False)
    # 普通整班 8h → 无加班，不出现
    _seed(db, "T0003", "2026-05-06", "出料工", work_hours=8.0)
    run_recon_for_date(db, "2026-05-06")
    set_confirm(db, emp_id="T0001", date="2026-05-06", kind="hr", status="通过", actor="HR01")

    meta = build_overtime_detail(db, month="2026-05")
    assert meta["columns"][:6] == ["序号", "工号", "加班员工姓名", "加班日期", "加班时间(小时)", "加班原因"]
    assert meta["columns"][-1] == "加班现场负责人签名"

    # T0001 两条加班（延时4 + 加班记录4）；T0003 不出现
    by_emp = [r for r in meta["rows"] if r[1] == "T0001"]
    ot_hours = sorted(r[4] for r in by_emp)
    assert ot_hours == [4.0, 4.0]
    assert not any(r[1] == "T0003" for r in meta["rows"])
    # 序号连续
    assert [r[0] for r in meta["rows"]] == list(range(1, len(meta["rows"]) + 1))

    # 汇总：按 工作点编号×加班岗位×班次 求和
    assert meta["summaryColumns"] == ["工作点编号", "加班岗位", "班次", "加班合计(小时)"]
    summ = {(r[0], r[1], r[2]): r[3] for r in meta["summaryRows"]}
    # T0001 出料工/中班 延时4 + 供气工/晚班 加班4
    assert summ[("35号工作点", "出料工", "2班")] == 4.0
    assert summ[("35号工作点", "供气工", "3班")] == 4.0

    # 按人汇总：T0001 两个不同(工作点/班次/岗位)组合 → 横向2对(小时1/岗位1、小时2/岗位2)
    assert meta["byPersonColumns"][:3] == ["序号", "工号", "加班员工姓名"]
    assert meta["byPersonColumns"][3:7] == ["加班小时1", "加班岗位1", "加班小时2", "加班岗位2"]
    prow = next(r for r in meta["byPersonRows"] if r[1] == "T0001")
    # 两对：(4, 35号工作点2班出料工) 和 (4, 35号工作点3班供气工)，顺序按岗位标签排序
    pairs = {prow[4]: prow[3], prow[6]: prow[5]}
    assert pairs["35号工作点2班出料工"] == 4.0
    assert pairs["35号工作点3班供气工"] == 4.0

    blob = build_overtime_detail_xlsx(meta)
    assert blob[:2] == b"PK"
    wb = openpyxl.load_workbook(io.BytesIO(blob))
    assert "加班汇总" in wb.sheetnames  # 第二个工作表存在
    assert wb["加班明细2026-05"]["B2"].value == "T0001"
    assert wb["加班明细2026-05"]["B2"].number_format == "@"
    assert wb["按人汇总"]["B2"].value == "T0001"
    assert wb["按人汇总"]["B2"].number_format == "@"


def test_overtime_excludes_invalid_day(db_session):
    db = db_session
    # 出勤 12h 但无劳勤覆盖 → 对账无效 → 延时加班不计入
    _seed(db, "T0005", "2026-05-07", "出料工", work_hours=12.0, reason="x", cover=False)
    meta = build_overtime_detail(db, month="2026-05")
    assert not any(r[1] == "T0005" for r in meta["rows"])


def test_overtime_includes_same_squad_when_base_attendance_is_valid(db_session):
    db = db_session
    _seed(db, "T0001", "2026-05-08", "出料工", squad="2班", cover=True)
    _seed(
        db,
        "T0001",
        "2026-05-08",
        "物料转运工",
        kind="加班",
        work_hours=4.0,
        squad="2班",
        cover=False,
    )

    meta = build_overtime_detail(db, month="2026-05")
    matching = [row for row in meta["rows"] if row[1] == "T0001"]
    assert len(matching) == 1
    assert matching[0][4] == 4.0
    assert matching[0][6] == "物料转运工"


def test_overtime_bad_month(db_session):
    with pytest.raises(ExportInputError):
        build_overtime_detail(db_session, month="2026-13")
