from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from app.models import AttendanceRecord, Furnace, ExternalAttendanceResultRaw, LaoqinPunch
from app.services.export_service import (
    MONTHLY_HOURS_COLUMNS,
    build_monthly_hours_xlsx,
    build_monthly_valid_hours,
)

_SH = ZoneInfo("Asia/Shanghai")


def _seed(db, emp, date, post, employ_type, *, cover=True, work_hours=8.0, furnace_no="35号工作点"):
    """加一条 早班(02:00-10:00) 扫码出勤；cover=True 时配覆盖打卡→自动一致(有效)，
    cover=False 时无打卡→工作点有劳勤无(无效)。"""
    f = db.query(Furnace).filter_by(furnace_no=furnace_no).first()
    y, m, d = (int(x) for x in date.split("-"))
    db.add(
        AttendanceRecord(
            emp_id=emp, furnace_id=f.id, squad="1班", post=post,
            employ_type=employ_type, work_hours=work_hours,
            scan_time=datetime(y, m, d, 8, tzinfo=_SH),
            source="扫码", natual_date=date,
        )
    )
    if cover:
        for h, mi, direct in ((1, 50, "进"), (10, 10, "出")):
            db.add(
                LaoqinPunch(
                    emp_id=emp, punch_time=datetime(y, m, d, h, mi, tzinfo=_SH),
                    inout_type=direct, natual_date=date,
                )
            )
        if db.query(ExternalAttendanceResultRaw).filter_by(user_code=emp, kq_date=date).first() is None:
            db.add(ExternalAttendanceResultRaw(
                user_code=emp, user_name=emp, kq_date=date,
                status_name="正常", cqgs=f"{work_hours:.5f}",
            ))
    db.commit()


def test_monthly_valid_hours_aggregates(db_session):
    # T0001：出料工/正式 两天=16h；加氧工/正式 一天=8h；出料工/实习 一天=8h（均有效）
    _seed(db_session, "T0001", "2026-05-06", "出料工", "正式")
    _seed(db_session, "T0001", "2026-05-07", "出料工", "正式")
    _seed(db_session, "T0001", "2026-05-06", "加氧工", "正式")
    _seed(db_session, "T0001", "2026-05-08", "出料工", "实习")
    # T0003：扫码无打卡→无效，应被排除
    _seed(db_session, "T0003", "2026-05-06", "出料工", "正式", cover=False)

    meta = build_monthly_valid_hours(db_session, month="2026-05")
    assert meta["columns"] == MONTHLY_HOURS_COLUMNS
    # rows: [工号, 姓名, 工时类型, 地区, 工作点编号, 岗位, 总工时, 折合天数]
    by_key = {(r[0], r[2], r[5]): r for r in meta["rows"]}

    out = by_key[("T0001", "正式", "出料工")]
    assert out[3] == "示例厂区A" and out[4] == "35号工作点"  # 地区/工作点编号
    assert out[6] == 16  # 总工时
    assert out[7] == 2  # 折合天数 16/8
    assert by_key[("T0001", "正式", "加氧工")][6] == 8
    assert by_key[("T0001", "实习", "出料工")][6] == 8
    assert by_key[("T0001", "实习", "出料工")][7] == 1
    # 无效的 T0003 不出现
    assert not any(r[0] == "T0003" for r in meta["rows"])

    # xlsx 可生成（表头 + 数据行）
    blob = build_monthly_hours_xlsx(meta)
    assert blob[:2] == b"PK"  # xlsx = zip
    ws = load_workbook(BytesIO(blob))["班次有效工时"]
    assert ws["A2"].value == "T0001"
    assert ws["A2"].data_type == "s"
    assert ws["A2"].number_format == "@"


def test_monthly_valid_hours_splits_by_furnace(db_session):
    # 同员工/工时类型/岗位，但不同工作点编号 → 拆成两条（新增的 工作点编号 维度）
    _seed(db_session, "T0001", "2026-05-06", "出料工", "正式", furnace_no="35号工作点")
    _seed(db_session, "T0001", "2026-05-06", "出料工", "正式", furnace_no="11号工作点")

    meta = build_monthly_valid_hours(db_session, month="2026-05")
    by_furnace = {r[4]: r for r in meta["rows"] if r[0] == "T0001" and r[5] == "出料工"}
    assert set(by_furnace) == {"35号工作点", "11号工作点"}
    assert by_furnace["35号工作点"][6] == 8
    assert by_furnace["11号工作点"][6] == 8
    blob = build_monthly_hours_xlsx(meta)
    assert blob[:2] == b"PK"  # xlsx = zip


def test_monthly_valid_hours_other_month_empty(db_session):
    _seed(db_session, "T0001", "2026-05-06", "出料工", "正式")
    meta = build_monthly_valid_hours(db_session, month="2026-06")
    assert meta["rows"] == []


def test_monthly_valid_hours_bad_month(db_session):
    from app.services.export_service import ExportInputError

    import pytest

    with pytest.raises(ExportInputError):
        build_monthly_valid_hours(db_session, month="2026-13")
